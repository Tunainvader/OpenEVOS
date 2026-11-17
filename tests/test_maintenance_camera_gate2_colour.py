"""Colour Camera Gate 2 (ADR-0038) against a stateful fake ueye_api DLL: never touches hardware."""

import ctypes as C
import json

import numpy as np
import pytest
from test_backend_ueye_gate import FakeUeye, Pnp, o

from openevos.backends.ueye import sdk as u
from openevos.maintenance import camera_gate2_colour as g2

SERIAL = "4002000000"


class StatefulUeye(FakeUeye):
    """Session state reset by is_InitCamera; raw modes in `supported`; dark raw frames at bias 40 + noise."""

    def __init__(self, supported=(29,), caps=3, **kw):
        super().__init__(**kw)
        self.supported, self.timeouts, self.bias, self.caps = set(supported), 0, 40, caps
        self._defaults()
        self.rng = np.random.default_rng(1)

    def _defaults(self):
        self.mode, self.exposure, self.gains = 1, 199.693125, [0, 5, 0, 3]
        self.bl_mode, self.bl_offset = 1, 0

    def is_InitCamera(self, ph, hwnd):
        rc = super().is_InitCamera(ph, hwnd)
        self._defaults()
        return rc

    def is_SetColorMode(self, h, mode):
        self._rec("is_SetColorMode", mode)
        if mode == u.IS_GET_COLOR_MODE:
            return self.mode
        if mode not in self.supported:
            return u.IS_INVALID_COLOR_FORMAT
        self.mode = mode
        return 0

    def is_Exposure(self, h, cmd, p, size):
        self._rec("is_Exposure", cmd)
        x = o(p)
        if cmd == u.EXPOSURE_SET:
            self.exposure = round(x.value / 0.101625) * 0.101625
        elif isinstance(x, C.Array):
            x[0], x[1], x[2] = 0.101625, 199.693125, 0.101625
        elif cmd == u.EXPOSURE_GET:
            x.value = self.exposure
        else:
            x.value = 0.0
        return 0

    def is_SetHardwareGain(self, h, m, r, g_, b):
        if m >= 0x8000:
            return self.gains[m - 0x8000]
        self.gains[0] = m
        return 0

    def is_Blacklevel(self, h, cmd, p, size):
        self._rec("is_Blacklevel", cmd)
        x = o(p)
        if cmd == u.BL_GET_OFFSET_RANGE:
            x[0], x[1], x[2] = 0, 255, 1
        elif cmd == u.BL_SET_MODE:
            if not self.caps & 1:
                return 155  # IS_NOT_SUPPORTED, as the real UI-146xLE (run 1)
            self.bl_mode = x.value
        elif cmd == u.BL_SET_OFFSET:
            self.bl_offset = x.value
        else:
            x.value = {u.BL_GET_CAPS: self.caps, u.BL_GET_MODE: self.bl_mode, u.BL_GET_MODE_DEFAULT: 1,
                       u.BL_GET_OFFSET: self.bl_offset, u.BL_GET_OFFSET_DEFAULT: 0}[cmd]
        return 0

    def is_SetGainBoost(self, h, m):
        return 0

    def is_AllocImageMem(self, h, w, hh, bpp, pmem, pid):
        self.pitch = w * bpp // 8
        buf = (C.c_ubyte * (self.pitch * hh))()
        if bpp == 16:
            a = np.frombuffer(buf, dtype="<u2").reshape(hh, w)
            a[:] = np.round(self.bias + self.bl_offset + self.rng.normal(0, 2, (hh, w))).clip(0).astype(np.uint16) * 64
        elif bpp == 8:
            a = np.frombuffer(buf, dtype=np.uint8).reshape(hh, w)
            a[:] = np.round(self.bl_offset / 4 + self.rng.normal(0, 1, (hh, w))).clip(0, 255).astype(np.uint8)
        self.bufs[7] = buf
        o(pmem).value, o(pid).value = C.addressof(buf), 7
        return 0

    def is_FreezeVideo(self, h, wait):
        self._rec("is_FreezeVideo", wait)
        if self.timeouts:
            self.timeouts -= 1
            return u.IS_TIMED_OUT
        return 0

    def is_GetImageMemPitch(self, h, p):
        o(p).value = self.pitch
        return 0


def gate1_record(d, fake):
    b = u.UeyeGateBackend(dll_path="x", loader=lambda p: fake)
    b.connect()
    rec = {"identity": b.identity(), "modes": b.modes(), "steps_done": ["connect", "identity", "modes"]}
    b.close()
    fake.calls.clear()
    (d / "camera-gate1-colour-20261115T142903-complete.json").write_text(json.dumps(rec), encoding="utf-8")


def run(tmp_path, fake, **kw):
    gate1_record(tmp_path, fake)
    b = u.UeyeSettingsBackend(dll_path="x", loader=lambda p: fake)
    return g2.run_gate2(Pnp(), b, evidence_dir=tmp_path, send=True, confirm=g2.CONFIRM_PHRASE,
                        busy=lambda: False, **kw)


def test_pass_raw16_unclipped_and_restored(tmp_path):
    fake = StatefulUeye()
    r = run(tmp_path, fake)
    assert r.outcome == "complete" and r.passed, r.message
    assert r.color_modes_tried == [{"code": 29, "name": "SENSOR_RAW16", "accepted": True}]
    assert r.settings[1]["blacklevel"]["read_back"] == {"mode": 0, "offset": 64}  # 0 + 25 % of 0..255
    assert set(r.exposures) == {"1ms", "10ms", "50ms"} and r.exposures["10ms"]["histogram_step"] == 64
    assert r.exposures["1ms"]["shape"] == [1536, 2048] and "p00" in r.exposures["1ms"]["planes"]
    assert r.restore_verified and r.restore_diffs == []
    assert fake.mode == 1 and fake.bl_offset == 0 and fake.exited  # driver defaults after re-init
    assert len(r.frame_files) == 3 and np.load(tmp_path / r.frame_files[0]).shape == (3, 1536, 2048)


def test_falls_back_through_raw_modes(tmp_path):
    fake = StatefulUeye(supported=())
    r = run(tmp_path, fake)
    assert r.outcome == "stopped" and "no raw Bayer mode" in r.message
    assert [t["accepted"] for t in r.color_modes_tried] == [False] * 4
    assert r.restore_verified  # re-init check still ran


def test_raw8_accepted_last(tmp_path):
    fake = StatefulUeye(supported=(11,))
    fake.bias = 40
    r = run(tmp_path, fake)
    assert [t["name"] for t in r.color_modes_tried][-1] == "SENSOR_RAW8"
    assert r.color_modes_tried[-1]["accepted"]


def test_rev2_offset_only_camera_like_run1(tmp_path):
    """The real UI-146xLE: RAW8 only, black-level caps 2 (offset, no auto switch). Rev 2 skips SET_MODE."""
    fake = StatefulUeye(supported=(11,), caps=2)
    r = run(tmp_path, fake)
    assert r.passed, r.message
    bl = r.settings[1]["blacklevel"]
    assert bl["mode_settable"] is False and bl["read_back"] == {"mode": 1, "offset": 64}
    assert u.BL_SET_MODE not in [a[0] for n, a in fake.calls if n == "is_Blacklevel"]
    assert r.exposures["1ms"]["dtype"] == "uint8" and r.exposures["1ms"]["p0_1"] > 0


def test_no_offset_cap_sets_nothing(tmp_path):
    fake = StatefulUeye(caps=0)
    r = run(tmp_path, fake)
    assert r.outcome == "stopped" and "no settable black-level offset" in r.message
    assert not any(n == "is_SetColorMode" and a != (u.IS_GET_COLOR_MODE,) for n, a in fake.calls)


def test_timeouts_skipped_up_to_three(tmp_path):
    fake = StatefulUeye()
    fake.timeouts = 2
    r = run(tmp_path, fake)
    assert r.passed and r.exposures["1ms"]["grabs_skipped"] == 2


def test_fourth_timeout_stops_and_restores(tmp_path):
    fake = StatefulUeye()
    fake.timeouts = 4
    r = run(tmp_path, fake)
    assert r.outcome == "stopped" and "1 ms" in r.message and r.restore_verified


def test_state_mismatch_changes_nothing(tmp_path):
    fake = StatefulUeye()
    gate1_record(tmp_path, fake)
    fake._defaults = lambda: (setattr(fake, "mode", 1), setattr(fake, "exposure", 50.0),
                              setattr(fake, "gains", [0, 5, 0, 3]), setattr(fake, "bl_mode", 1),
                              setattr(fake, "bl_offset", 0))
    b = u.UeyeSettingsBackend(dll_path="x", loader=lambda p: fake)
    r = g2.run_gate2(Pnp(), b, evidence_dir=tmp_path, send=True, confirm=g2.CONFIRM_PHRASE, busy=lambda: False)
    assert r.outcome == "stopped" and "differs from the Gate 1 record" in r.message
    assert not any(n == "is_SetColorMode" and a != (u.IS_GET_COLOR_MODE,) for n, a in fake.calls)


def test_refuses_while_app_running(tmp_path):
    fake = StatefulUeye()
    gate1_record(tmp_path, fake)
    b = u.UeyeSettingsBackend(dll_path="x", loader=lambda p: fake)
    r = g2.run_gate2(Pnp(), b, evidence_dir=tmp_path, send=True, confirm=g2.CONFIRM_PHRASE, busy=lambda: True)
    assert r.outcome == "refused" and "app is running" in r.message
    assert {n for n, _ in fake.calls} <= {"is_GetNumberOfCameras"}


def test_dry_run_and_wrong_phrase_open_nothing(tmp_path):
    fake = StatefulUeye()
    gate1_record(tmp_path, fake)
    b = u.UeyeSettingsBackend(dll_path="x", loader=lambda p: fake)
    assert g2.run_gate2(Pnp(), b, evidence_dir=tmp_path, busy=lambda: False).outcome == "dry_run"
    r = g2.run_gate2(Pnp(), b, evidence_dir=tmp_path, send=True, confirm="yes", busy=lambda: False)
    assert r.outcome == "refused"
    assert {n for n, _ in fake.calls} <= {"is_GetNumberOfCameras"}


def test_setter_guards():
    b = u.UeyeSettingsBackend(dll_path="x", loader=lambda p: StatefulUeye())
    h = C.c_uint(1)
    with pytest.raises(u.UeyeError, match="refused"):
        b._call("is_SetColorMode", h, 1)  # BGR8 is not a raw candidate
    with pytest.raises(u.UeyeError, match="refused"):
        b._call("is_Exposure", h, u.EXPOSURE_SET, C.byref(C.c_double(20.0)), 8)
    with pytest.raises(u.UeyeError, match="refused"):
        b._call("is_SetHardwareGain", h, 10, -1, -1, -1)
    with pytest.raises(u.UeyeError, match="refused"):
        b._call("is_SetHardwareGain", h, 0, 0, 0, 0)  # R/G/B untouched
    with pytest.raises(u.UeyeError, match="refused"):
        b._call("is_Blacklevel", h, u.BL_SET_MODE, C.byref(C.c_int(1)), 4)  # auto on
    with pytest.raises(u.UeyeError, match="refused"):
        b._call("is_Blacklevel", h, u.BL_SET_OFFSET, C.byref(C.c_int(64)), 4)  # no offset computed yet
    b.allowed_offset = 64
    with pytest.raises(u.UeyeError, match="refused"):
        b._call("is_Blacklevel", h, u.BL_SET_OFFSET, C.byref(C.c_int(65)), 4)
    with pytest.raises(u.UeyeError, match="refused"):
        b._call("is_PixelClock", h, 6, C.byref(C.c_uint(30)), 4)  # pixel clock SET
    with pytest.raises(u.UeyeError, match="not in the reviewed"):
        b._call("is_ParameterSet", h)


def test_offset_target():
    assert u.offset_target(0, [0, 255, 1]) == 64
    assert u.offset_target(250, [0, 255, 1]) == 255
    assert u.offset_target(10, [0, 100, 4]) == 36
