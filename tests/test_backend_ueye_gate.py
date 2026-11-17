"""uEye Gate 1 adapter (colour camera) against a fake ueye_api DLL: never touches hardware."""

import ctypes as C

import numpy as np
import pytest

from openevos.backends.ueye import sdk as u
from openevos.maintenance import camera_gate1 as g


def o(p):
    return getattr(p, "_obj", p)


class FakeUeye:
    """Records calls; serves a 2048x1536 BGR8 frame (dark, value 3, one bright pixel)."""

    W, H, PITCH = 2048, 1536, 2048 * 3

    def __init__(self, n=1, in_use=False, init_rc=0, mode=1):
        self.n, self.in_use, self.init_rc, self.mode = n, in_use, init_rc, mode
        self.calls, self.bufs, self.freed, self.exited = [], {}, [], False

    def __getattr__(self, name):
        raise AttributeError(name)

    def _rec(self, name, *a):
        self.calls.append((name, a))

    def is_GetDLLVersion(self):
        self._rec("is_GetDLLVersion")
        return (4 << 24) | (97 << 16) | 4111

    def is_GetNumberOfCameras(self, p):
        self._rec("is_GetNumberOfCameras")
        o(p).value = self.n
        return 0

    def is_GetCameraList(self, p):
        lst = o(p)
        for i in range(lst.dwCount):
            c = lst.uci[i]
            c.dwCameraID, c.dwDeviceID, c.dwSensorID, c.dwInUse = 1, 1, 0x1465, int(self.in_use)
            c.SerNo, c.Model, c.FullModelName = b"4002000000", b"UI146xLE-C", b"UI-1460LE-C"
        return 0

    def is_InitCamera(self, ph, hwnd):
        self._rec("is_InitCamera", o(ph).value, hwnd)
        if self.init_rc:
            return self.init_rc
        o(ph).value = 1
        return 0

    def is_ExitCamera(self, h):
        self.exited = True
        return 0

    def is_GetCameraInfo(self, h, p):
        b = o(p)
        b.SerNo, b.ID, b.Version, b.Date, b.Type = b"4002000000", b"IDS GmbH", b"V2.10", b"01.01.2012", 4
        return 0

    def is_GetSensorInfo(self, h, p):
        s = o(p)
        s.SensorID, s.strSensorName, s.nColorMode = 0x0016, b"UI146xLE-C", b"\x02"
        s.nMaxWidth, s.nMaxHeight, s.wPixelSize, s.bMasterGain = 2048, 1536, 320, 1
        return 0

    def is_GetError(self, h, pc, pt):
        return 0

    def is_SetColorMode(self, h, mode):
        self._rec("is_SetColorMode", mode)
        return self.mode

    def is_AOI(self, h, cmd, p, size):
        r = o(p)
        r.s32X, r.s32Y, r.s32Width, r.s32Height = 0, 0, self.W, self.H
        return 0

    def is_Exposure(self, h, cmd, p, size):
        self._rec("is_Exposure", cmd)
        x = o(p)
        if isinstance(x, C.Array):
            x[0], x[1], x[2] = 0.04, 200.0, 0.01
        else:
            x.value = 33.0
        return 0

    def is_PixelClock(self, h, cmd, p, size):
        x = o(p)
        if isinstance(x, C.Array):
            x[0], x[1], x[2] = 5, 30, 1
        else:
            x.value = 24
        return 0

    def is_SetFrameRate(self, h, fps, p):
        assert isinstance(fps, C.c_double)
        o(p).value = 12.0
        return 0

    def is_SetHardwareGain(self, h, which, r, g_, b):
        return 0 if which == u.IS_GET_MASTER_GAIN else 13

    def is_SetAutoParameter(self, h, which, a, b):
        o(a).value = 0.0
        return 0

    def is_AllocImageMem(self, h, w, hh, bpp, pmem, pid):
        buf = (C.c_ubyte * (self.PITCH * hh))()
        arr = np.frombuffer(buf, dtype=np.uint8).reshape(hh, self.PITCH)
        arr[:] = 3
        arr[10, 30:33] = (255, 128, 0)  # B, G, R at pixel (10, 10)
        self.bufs[7] = buf
        o(pmem).value = C.addressof(buf)
        o(pid).value = 7
        return 0

    def is_SetImageMem(self, h, mem, mid):
        return 0

    def is_FreezeVideo(self, h, wait):
        self._rec("is_FreezeVideo", wait)
        return 0

    def is_GetImageMemPitch(self, h, p):
        o(p).value = self.PITCH
        return 0

    def is_FreeImageMem(self, h, mem, mid):
        self.freed.append(getattr(mid, "value", mid))
        return 0


def backend(fake):
    return u.UeyeGateBackend(dll_path="fake.dll", loader=lambda p: fake)


class Pnp:
    def present(self, vid, pid):
        return [g.PnpDevice("USB\\VID_1409&PID_1465\\X", vid, pid, 0)] if (vid, pid) == (0x1409, 0x1465) else []


COLOUR = g.ROLES["colour"]


def test_full_gate_reads_identity_modes_and_one_frame(tmp_path):
    fake = FakeUeye()
    r = g.run_gate(COLOUR, Pnp(), backend(fake), send=True, confirm=COLOUR.phrase, evidence_dir=tmp_path)
    assert r.steps_done == ["connect", "identity", "modes", "grab_one", "close"], r.message
    assert r.identity["full_model"] == "UI-1460LE-C" and r.identity["max_size"] == [2048, 1536]
    assert r.identity["library_version"] == "4.97.4111" and r.identity["pixel_size_um"] == pytest.approx(3.2)
    assert r.modes["color_mode"]["name"] == "BGR8_PACKED" and r.modes["exposure_ms"] == 33.0
    assert r.modes["exposure_range_ms"] == [0.04, 200.0, 0.01] and r.modes["pixel_clock_mhz"] == 24
    assert r.modes["frame_rate_fps"] == 12.0 and r.modes["gain"]["red"] == 13
    assert r.frame_stats["shape"] == [1536, 2048, 3] and r.frame_stats["saturated"] == 1
    px = np.load(tmp_path / r.frame_file)
    assert tuple(px[10, 10]) == (0, 128, 255)  # BGR in memory -> RGB
    assert fake.exited and fake.freed == [7]


def test_init_never_passes_upload_or_device_id_flags():
    fake = FakeUeye()
    b = backend(fake)
    b.connect()
    init = [a for n, a in fake.calls if n == "is_InitCamera"]
    assert init == [(0, None)]
    b.close()


def test_guards_refuse_setters():
    b = backend(FakeUeye())
    h = C.c_uint(1)
    with pytest.raises(u.UeyeError, match="refused"):
        b._call("is_SetColorMode", h, 1)  # set BGR8
    with pytest.raises(u.UeyeError, match="refused"):
        b._call("is_Exposure", h, 12, C.byref(C.c_double(5.0)), 8)  # SET_EXPOSURE
    with pytest.raises(u.UeyeError, match="refused"):
        b._call("is_PixelClock", h, 6, C.byref(C.c_uint(30)), 4)  # SET
    with pytest.raises(u.UeyeError, match="refused"):
        b._call("is_SetHardwareGain", h, 50, -1, -1, -1)
    with pytest.raises(u.UeyeError, match="refused"):
        b._call("is_SetAutoParameter", h, 0x8802, C.byref(C.c_double(1)), C.byref(C.c_double(0)))
    with pytest.raises(u.UeyeError, match="refused"):
        b._call("is_InitCamera", C.byref(C.c_uint(u.STARTER_UPLOAD_FLAG)), None)
    with pytest.raises(u.UeyeError, match="not in the reviewed"):
        b._call("is_SaveParameters", h)


def test_starter_upload_request_stops_gate(tmp_path):
    fake = FakeUeye(init_rc=u.STARTER_UPLOAD_NEEDED)
    r = g.run_gate(COLOUR, Pnp(), backend(fake), send=True, confirm=COLOUR.phrase)
    assert r.outcome == "stopped" and "184" in r.message and not fake.exited


def test_camera_in_use_is_not_opened():
    fake = FakeUeye(in_use=True)
    r = g.run_gate(COLOUR, Pnp(), backend(fake), send=True, confirm=COLOUR.phrase)
    assert r.outcome == "stopped" and "in use" in r.message
    assert not any(n == "is_InitCamera" for n, _ in fake.calls)


def test_dry_run_opens_nothing():
    fake = FakeUeye()
    r = g.run_gate(COLOUR, Pnp(), backend(fake))
    assert r.outcome == "dry_run"
    assert {n for n, _ in fake.calls} <= {"is_GetNumberOfCameras"}


def test_unknown_colour_mode_takes_no_frame():
    fake = FakeUeye(mode=12)  # UYVY
    b = backend(fake)
    b.connect()
    with pytest.raises(u.UeyeError, match="cannot decode|not one the gate"):
        b.grab_one(5.0)
    b.close()
    assert not any(n == "is_FreezeVideo" for n, _ in fake.calls)


def test_raw12_frame_decoded_as_uint16():
    raw = np.arange(6, dtype="<u2").tobytes()  # 2 rows x 2 px, pitch 6 bytes (one padding pixel per row)
    px, bits = u.frame_from_buffer(raw, 2, 2, 6, 27)
    assert bits == 12 and px.dtype == np.uint16 and px.tolist() == [[0, 1], [3, 4]]
