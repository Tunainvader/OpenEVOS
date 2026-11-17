"""IDS uEye classic C API (`ueye_api_64.dll`, 4.97) via ctypes: read-only Camera Gate 1 adapter for the
colour camera, a uEye UI-146xLE (`1409:1465`; enumerates as `1409:1000` "uEye boot" until the driver
loads its RAM image at power-up).

Constant values come from IDS's own generated binding `pyueye/ueye.py` (IDS-maintained,
github.com/jkokorian/ids-pyueye, read 2026-11-15); the installed IDS peak 26.06 has no uEye.h.

Safety by construction:
* Only functions in `GUARDS` can be called, and each call's command/argument must pass its guard: the
  `is_Set*` functions are callable only with their GET query constants; `is_Exposure`, `is_PixelClock`
  and `is_AOI` only with GET commands. No parameter-set, EEPROM, LUT, I/O, trigger or update call is
  bound.
* `is_InitCamera` is called with handle value 0 (first free camera) and never with the starter-image
  upload flag (0x10000) or the device-ID flag; a camera asking for that upload (error 184) stops the
  gate.
* Output structs carry an 8 KB trailing pad against layout differences between SDK versions.
"""

from __future__ import annotations

import ctypes as C
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import numpy as np

DEFAULT_DLL = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "ueye_api_64.dll"
PAD = 8192

IS_SUCCESS = 0
IS_NO_SUCCESS = -1
IS_TIMED_OUT = 122
STARTER_UPLOAD_NEEDED = 184  # IS_STARTER_FW_UPLOAD_NEEDED
STARTER_UPLOAD_FLAG = 0x10000  # IS_ALLOW_STARTER_FW_UPLOAD: never passed
USE_DEVICE_ID_FLAG = 0x8000  # IS_USE_DEVICE_ID: never passed
IS_IGNORE_PARAMETER = -1

IS_GET_COLOR_MODE = 0x8000
IS_GET_MASTER_GAIN, IS_GET_RED_GAIN, IS_GET_GREEN_GAIN, IS_GET_BLUE_GAIN = 0x8000, 0x8001, 0x8002, 0x8003
AUTO_GETS = {"auto_gain": 0x8801, "auto_shutter": 0x8803, "auto_whitebalance": 0x8805, "auto_framerate": 0x8807}
IS_GET_FRAMERATE = 0x8000
IS_AOI_IMAGE_GET_AOI = 0x0002
EXPOSURE_GET, EXPOSURE_GET_DEFAULT, EXPOSURE_GET_RANGE = 7, 2, 6  # SET is 12: refused by the guard
PIXELCLOCK_GET, PIXELCLOCK_GET_RANGE, PIXELCLOCK_GET_DEFAULT = 5, 3, 4  # SET is 6: refused

# colour mode -> (name, bits per pixel in memory, data bits, channels)
COLOR_MODES = {
    0: ("BGRA8_PACKED", 32, 8, 4), 0x80: ("RGBA8_PACKED", 32, 8, 4),
    1: ("BGR8_PACKED", 24, 8, 3), 0x81: ("RGB8_PACKED", 24, 8, 3),
    6: ("MONO8", 8, 8, 1), 11: ("SENSOR_RAW8", 8, 8, 1),
    34: ("MONO10", 16, 10, 1), 26: ("MONO12", 16, 12, 1), 28: ("MONO16", 16, 16, 1),
    33: ("SENSOR_RAW10", 16, 10, 1), 27: ("SENSOR_RAW12", 16, 12, 1), 29: ("SENSOR_RAW16", 16, 16, 1),
}


def _arg(args: tuple, i: int) -> int:
    a = args[i]
    return int(a.value) if hasattr(a, "value") else int(a)


# function -> guard over the positional arguments (True = allowed)
GUARDS: dict[str, Any] = {
    "is_GetDLLVersion": lambda a: True,
    "is_GetNumberOfCameras": lambda a: True,
    "is_GetCameraList": lambda a: True,
    "is_InitCamera": lambda a: _handle_in(a[0]) == 0 and a[1] is None,
    "is_ExitCamera": lambda a: True,
    "is_GetCameraInfo": lambda a: True,
    "is_GetSensorInfo": lambda a: True,
    "is_GetError": lambda a: True,
    "is_SetColorMode": lambda a: _arg(a, 1) == IS_GET_COLOR_MODE,
    "is_SetHardwareGain": lambda a: _arg(a, 1) in (IS_GET_MASTER_GAIN, IS_GET_RED_GAIN, IS_GET_GREEN_GAIN,
                                                    IS_GET_BLUE_GAIN) and all(_arg(a, i) == IS_IGNORE_PARAMETER
                                                                              for i in (2, 3, 4)),
    "is_SetAutoParameter": lambda a: _arg(a, 1) in AUTO_GETS.values(),
    "is_SetFrameRate": lambda a: _arg(a, 1) == IS_GET_FRAMERATE,
    "is_AOI": lambda a: _arg(a, 1) == IS_AOI_IMAGE_GET_AOI,
    "is_Exposure": lambda a: _arg(a, 1) in (EXPOSURE_GET, EXPOSURE_GET_DEFAULT, EXPOSURE_GET_RANGE),
    "is_PixelClock": lambda a: _arg(a, 1) in (PIXELCLOCK_GET, PIXELCLOCK_GET_RANGE, PIXELCLOCK_GET_DEFAULT),
    # host-memory buffer for the one frame (allocation/selection of PC RAM, nothing on the camera)
    "is_AllocImageMem": lambda a: True,
    "is_SetImageMem": lambda a: True,
    "is_GetImageMemPitch": lambda a: True,
    "is_FreeImageMem": lambda a: True,
    "is_FreezeVideo": lambda a: 10 <= _arg(a, 1) <= 32768,  # timeout in 10 ms units
}
# functions whose return value is data, not a status code
RETURNS_VALUE = {"is_GetDLLVersion", "is_SetColorMode", "is_SetHardwareGain"}


def _handle_in(p) -> int:
    obj = getattr(p, "_obj", p)  # ctypes byref() wrapper
    return int(obj.value)


class BOARDINFO(C.Structure):
    _fields_ = [("SerNo", C.c_char * 12), ("ID", C.c_char * 20), ("Version", C.c_char * 10),
                ("Date", C.c_char * 12), ("Select", C.c_ubyte), ("Type", C.c_ubyte), ("Reserved", C.c_char * 8),
                ("_pad", C.c_ubyte * PAD)]


class SENSORINFO(C.Structure):
    _fields_ = [("SensorID", C.c_ushort), ("strSensorName", C.c_char * 32), ("nColorMode", C.c_char),
                ("nMaxWidth", C.c_uint), ("nMaxHeight", C.c_uint), ("bMasterGain", C.c_int), ("bRGain", C.c_int),
                ("bGGain", C.c_int), ("bBGain", C.c_int), ("bGlobShutter", C.c_int), ("wPixelSize", C.c_ushort),
                ("nUpperLeftBayerPixel", C.c_char), ("Reserved", C.c_char * 13), ("_pad", C.c_ubyte * PAD)]


class UEYE_CAMERA_INFO(C.Structure):
    _fields_ = [("dwCameraID", C.c_uint), ("dwDeviceID", C.c_uint), ("dwSensorID", C.c_uint),
                ("dwInUse", C.c_uint), ("SerNo", C.c_char * 16), ("Model", C.c_char * 16), ("dwStatus", C.c_uint),
                ("dwReserved", C.c_uint * 2), ("FullModelName", C.c_char * 32), ("dwReserved2", C.c_uint * 5)]


def camera_list_type(n: int):
    class UEYE_CAMERA_LIST(C.Structure):
        _fields_ = [("dwCount", C.c_uint), ("uci", UEYE_CAMERA_INFO * max(1, n)), ("_pad", C.c_ubyte * PAD)]
    return UEYE_CAMERA_LIST


class IS_RECT(C.Structure):
    _fields_ = [("s32X", C.c_int), ("s32Y", C.c_int), ("s32Width", C.c_int), ("s32Height", C.c_int)]


class UeyeError(RuntimeError):
    code: int | None = IS_NO_SUCCESS


def _s(b: bytes) -> str:
    return b.split(b"\0", 1)[0].decode("ascii", "replace").strip()


def _byte(c) -> int:
    return c[0] if isinstance(c, (bytes, bytearray)) and c else int.from_bytes(c, "little") if c else 0


def frame_from_buffer(buf: bytes, width: int, height: int, pitch: int, mode: int) -> tuple[np.ndarray, int]:
    """Copy one frame out of the SDK buffer: 8-bit colour -> (H, W, 3) RGB uint8; mono/raw -> (H, W)."""
    name, bpp, bits, ch = COLOR_MODES[mode]
    rows = np.frombuffer(buf, dtype=np.uint8, count=pitch * height).reshape(height, pitch)
    line = rows[:, : width * bpp // 8]
    if bpp == 16:
        return line.copy().view("<u2").reshape(height, width), bits
    if ch == 1:
        return line.copy(), 8
    px = line.reshape(height, width, ch)[:, :, :3]
    if name.startswith("BGR"):
        px = px[:, :, ::-1]
    return np.ascontiguousarray(px), 8


class UeyeGateBackend:
    """Implements `camera_gate1.CameraGateBackend` with the uEye API's read-only calls."""

    def __init__(self, dll_path: Path = DEFAULT_DLL, loader=None):
        self.dll_path = Path(dll_path)
        self._loader = loader
        self._lib = None
        self._h = C.c_uint(0)
        self._open = False
        self._mem: tuple[C.c_void_p, C.c_int] | None = None
        self.calls: list[str] = []

    def _load(self):
        if self._lib is None:
            self._lib = self._loader(self.dll_path) if self._loader else C.CDLL(str(self.dll_path))
        return self._lib

    def _allowed(self, name: str, args: tuple) -> bool | None:
        """None = function not bound; False = arguments refused."""
        guard = GUARDS.get(name)
        return None if guard is None else bool(guard(args))

    def _call(self, name: str, *args) -> int:
        ok = self._allowed(name, args)
        if not ok:
            err = UeyeError(f"{name} is not in the reviewed set" if ok is None
                            else f"{name} refused: arguments are not a reviewed query or setting")
            err.code = None  # refused here; never reached the camera
            raise err
        fn = getattr(self._load(), name)
        rc = int(fn(*args))
        self.calls.append(name)
        if name in RETURNS_VALUE or rc == IS_SUCCESS:
            return rc
        err = UeyeError(f"{name} returned {rc}{self._error_text()}")
        err.code = rc
        raise err

    def _error_text(self) -> str:
        if not self._open:
            return ""
        try:
            code, txt = C.c_int(0), C.c_char_p()
            if int(self._load().is_GetError(self._h, C.byref(code), C.byref(txt))) == IS_SUCCESS and txt.value:
                return f": {txt.value.decode('ascii', 'replace')}"
        except Exception:  # noqa: BLE001, S110 - best effort
            pass
        return ""

    def sdk_available(self) -> bool:
        if self._loader is None and not self.dll_path.exists():
            return False
        try:
            self._load()
        except OSError:
            return False
        return True

    def dll_version(self) -> str:
        v = self._call("is_GetDLLVersion") & 0xFFFFFFFF
        return f"{v >> 24}.{(v >> 16) & 0xFF}.{v & 0xFFFF}"

    def count_cameras(self) -> int:
        """Driver enumeration only; opens no camera."""
        n = C.c_int(0)
        self._call("is_GetNumberOfCameras", C.byref(n))
        return n.value

    def camera_list(self) -> list[dict[str, Any]]:
        n = self.count_cameras()
        lst = camera_list_type(n)()
        lst.dwCount = n
        if n:
            self._call("is_GetCameraList", C.byref(lst))
        return [{"camera_id": c.dwCameraID, "device_id": c.dwDeviceID, "sensor_id": c.dwSensorID,
                 "in_use": bool(c.dwInUse), "serial": _s(c.SerNo), "model": _s(c.Model),
                 "full_model": _s(c.FullModelName), "status": c.dwStatus} for c in lst.uci[:n]]

    # -- gate steps ------------------------------------------------------------------------
    def connect(self) -> None:
        self._listed = self.camera_list()
        if any(c["in_use"] for c in self._listed):
            raise UeyeError("camera reported in use by another program; not opened")
        self._h = C.c_uint(0)  # first free camera; no starter-upload / device-id flags
        try:
            self._call("is_InitCamera", C.byref(self._h), None)
        except UeyeError as exc:
            if exc.code == STARTER_UPLOAD_NEEDED:
                raise UeyeError("camera asks for a starter image upload (184); refused, not opened") from exc
            raise
        self._open = True

    def identity(self) -> dict[str, Any]:
        bi, si = BOARDINFO(), SENSORINFO()
        self._call("is_GetCameraInfo", self._h, C.byref(bi))
        self._call("is_GetSensorInfo", self._h, C.byref(si))
        listed = self._listed[0] if getattr(self, "_listed", None) else {}
        return {"serial": _s(bi.SerNo), "id": _s(bi.ID), "version": _s(bi.Version), "date": _s(bi.Date),
                "type": bi.Type, "model": listed.get("model"), "full_model": listed.get("full_model"),
                "device_id": listed.get("device_id"), "sensor_id": si.SensorID, "sensor": _s(si.strSensorName),
                "sensor_color_mode": _byte(si.nColorMode), "max_size": [si.nMaxWidth, si.nMaxHeight],
                "pixel_size_um": si.wPixelSize / 100.0, "global_shutter": bool(si.bGlobShutter),
                "gains": {"master": bool(si.bMasterGain), "r": bool(si.bRGain), "g": bool(si.bGGain),
                          "b": bool(si.bBGain)},
                "upper_left_bayer": _byte(si.nUpperLeftBayerPixel), "library_version": self.dll_version()}

    def modes(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        mode = self._call("is_SetColorMode", self._h, IS_GET_COLOR_MODE)
        out["color_mode"] = {"code": mode, "name": COLOR_MODES.get(mode, (f"unknown {mode}",))[0]}
        r = IS_RECT()
        self._call("is_AOI", self._h, IS_AOI_IMAGE_GET_AOI, C.byref(r), C.sizeof(r))
        out["aoi"] = {"x": r.s32X, "y": r.s32Y, "width": r.s32Width, "height": r.s32Height}
        for key, cmd, typ in (("exposure_ms", EXPOSURE_GET, C.c_double),
                              ("exposure_default_ms", EXPOSURE_GET_DEFAULT, C.c_double)):
            v = typ()
            out[key] = self._try(lambda v=v, cmd=cmd: self._call("is_Exposure", self._h, cmd, C.byref(v),
                                                                 C.sizeof(v)) or v.value)
        rng = (C.c_double * 3)()
        out["exposure_range_ms"] = self._try(lambda: self._call("is_Exposure", self._h, EXPOSURE_GET_RANGE,
                                                                rng, C.sizeof(rng)) or list(rng))
        for key, cmd in (("pixel_clock_mhz", PIXELCLOCK_GET), ("pixel_clock_default_mhz", PIXELCLOCK_GET_DEFAULT)):
            v = C.c_uint()
            out[key] = self._try(lambda v=v, cmd=cmd: self._call("is_PixelClock", self._h, cmd, C.byref(v),
                                                                 C.sizeof(v)) or v.value)
        prng = (C.c_uint * 3)()
        out["pixel_clock_range_mhz"] = self._try(lambda: self._call("is_PixelClock", self._h, PIXELCLOCK_GET_RANGE,
                                                                    prng, C.sizeof(prng)) or list(prng))
        fps = C.c_double()
        # is_SetFrameRate(HIDS, double FPS, double* newFPS): the query constant goes in as a double
        out["frame_rate_fps"] = self._try(lambda: self._call("is_SetFrameRate", self._h,
                                                             C.c_double(IS_GET_FRAMERATE), C.byref(fps)) or fps.value)
        out["gain"] = {k: self._try(lambda c=c: self._call("is_SetHardwareGain", self._h, c, -1, -1, -1))
                       for k, c in (("master", IS_GET_MASTER_GAIN), ("red", IS_GET_RED_GAIN),
                                    ("green", IS_GET_GREEN_GAIN), ("blue", IS_GET_BLUE_GAIN))}
        auto = {}
        for k, c in AUTO_GETS.items():
            a, b = C.c_double(), C.c_double()
            auto[k] = self._try(lambda a=a, b=b, c=c: self._call("is_SetAutoParameter", self._h, c, C.byref(a),
                                                                 C.byref(b)) or bool(a.value))
        out["auto"] = auto
        return out

    @staticmethod
    def _try(fn):
        try:
            return fn()
        except UeyeError as exc:
            return {"error": str(exc)}

    def grab_one(self, timeout_s: float) -> tuple[np.ndarray, int]:
        mode = self._call("is_SetColorMode", self._h, IS_GET_COLOR_MODE)
        if mode not in COLOR_MODES:
            raise UeyeError(f"current colour mode {mode} is not one the gate can decode; no frame taken")
        r = IS_RECT()
        self._call("is_AOI", self._h, IS_AOI_IMAGE_GET_AOI, C.byref(r), C.sizeof(r))
        w, h, bpp = r.s32Width, r.s32Height, COLOR_MODES[mode][1]
        mem, mid = C.c_void_p(), C.c_int()
        self._call("is_AllocImageMem", self._h, w, h, bpp, C.byref(mem), C.byref(mid))
        self._mem = (mem, mid)
        try:
            self._call("is_SetImageMem", self._h, mem, mid)
            self._call("is_FreezeVideo", self._h, max(10, min(32768, int(timeout_s * 100))))
            pitch = C.c_int()
            self._call("is_GetImageMemPitch", self._h, C.byref(pitch))
            buf = C.string_at(mem, pitch.value * h)
            return frame_from_buffer(buf, w, h, pitch.value, mode)
        finally:
            self._free()

    def _free(self) -> None:
        if self._mem is not None:
            mem, mid = self._mem
            self._mem = None
            try:
                self._call("is_FreeImageMem", self._h, mem, mid)
            except UeyeError:
                pass

    def close(self) -> None:
        try:
            self._free()
        finally:
            if self._open:
                self._open = False
                self._call("is_ExitCamera", self._h)


# -- Gate 2 settings path (ADR-0038, spec camera_gate2_colour_settings_spec.md §4) ----------------------------
IS_INVALID_COLOR_FORMAT = 174
IS_TRANSFER_ERROR = 178
IS_GET_GAINBOOST = 0x8008
EXPOSURE_SET = 12
BL_GET_CAPS, BL_GET_MODE_DEFAULT, BL_GET_MODE, BL_SET_MODE = 1, 2, 3, 4
BL_GET_OFFSET_DEFAULT, BL_GET_OFFSET_RANGE, BL_GET_OFFSET, BL_SET_OFFSET = 5, 6, 7, 8
BL_GETS = (BL_GET_CAPS, BL_GET_MODE_DEFAULT, BL_GET_MODE, BL_GET_OFFSET_DEFAULT, BL_GET_OFFSET_RANGE, BL_GET_OFFSET)
IS_AUTO_BLACKLEVEL_OFF = 0
BL_CAP_SET_AUTO, BL_CAP_SET_OFFSET = 1, 2
RAW_CANDIDATES = (29, 27, 33, 11)  # SENSOR_RAW16, RAW12, RAW10, RAW8 (tried in this order)
EXPOSURES_MS = (1.0, 10.0, 50.0)
OFFSET_FRACTION = 0.25  # black-level offset = default + 25 % of the range
GRAB_SKIP_CODES = (IS_TIMED_OUT, IS_TRANSFER_ERROR)


def _deref(p) -> float:
    return float(getattr(p, "_obj", p).value)


class UeyeSettingsBackend(UeyeGateBackend):
    """Gate 1 adapter plus the guarded RAM-only setters of colour Gate 2. The only values that pass:
    a raw colour mode from RAW_CANDIDATES, master gain 0 with R/G/B untouched, black-level auto off,
    black-level offset == `allowed_offset` (computed by the gate from the range it read), and exposure
    1, 10 or 50 ms. Restore is is_ExitCamera; the gate verifies it with a fresh init."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.allowed_offset: int | None = None

    def _allowed(self, name: str, args: tuple) -> bool | None:
        if name == "is_SetColorMode":
            return _arg(args, 1) in (IS_GET_COLOR_MODE, *RAW_CANDIDATES)
        if name == "is_SetHardwareGain":
            return bool(super()._allowed(name, args)) or tuple(_arg(args, i) for i in (1, 2, 3, 4)) == (0, -1, -1, -1)
        if name == "is_Exposure":
            if _arg(args, 1) == EXPOSURE_SET:
                return _deref(args[2]) in EXPOSURES_MS
            return super()._allowed(name, args)
        if name == "is_Blacklevel":
            cmd = _arg(args, 1)
            if cmd in BL_GETS:
                return True
            if cmd == BL_SET_MODE:
                return int(_deref(args[2])) == IS_AUTO_BLACKLEVEL_OFF
            if cmd == BL_SET_OFFSET:
                return self.allowed_offset is not None and int(_deref(args[2])) == self.allowed_offset
            return False
        if name == "is_SetGainBoost":
            return _arg(args, 1) == IS_GET_GAINBOOST
        return super()._allowed(name, args)

    # -- reads ------------------------------------------------------------------------------
    def _bl_int(self, cmd: int) -> int:
        v = C.c_int()
        self._call("is_Blacklevel", self._h, cmd, C.byref(v), C.sizeof(v))
        return v.value

    def blacklevel(self) -> dict[str, Any]:
        rng = (C.c_int * 3)()
        out: dict[str, Any] = {k: self._try(lambda c=c: self._bl_int(c)) for k, c in (
            ("caps", BL_GET_CAPS), ("mode", BL_GET_MODE), ("mode_default", BL_GET_MODE_DEFAULT),
            ("offset", BL_GET_OFFSET), ("offset_default", BL_GET_OFFSET_DEFAULT))}
        out["offset_range"] = self._try(lambda: self._call("is_Blacklevel", self._h, BL_GET_OFFSET_RANGE, rng,
                                                           C.sizeof(rng)) or list(rng))
        return out

    def gain_boost(self):
        return self._try(lambda: self._call("is_SetGainBoost", self._h, IS_GET_GAINBOOST))

    def snapshot(self) -> dict[str, Any]:
        snap = self.modes()
        snap["blacklevel"] = self.blacklevel()
        snap["gain_boost"] = self.gain_boost()
        return snap

    # -- setters ------------------------------------------------------------------------------
    def try_color_mode(self, mode: int) -> bool:
        """True if accepted (and read back); False if the camera rejects the format (nothing changed)."""
        # is_SetColorMode returns data for the GET query but a status code for a set
        rc = self._call("is_SetColorMode", self._h, mode)
        if rc in (IS_INVALID_COLOR_FORMAT, IS_NO_SUCCESS, 155, 101, 125):  # format/mode/parameter/not supported
            return False
        if rc != IS_SUCCESS:
            raise UeyeError(f"is_SetColorMode({mode}) returned {rc}")
        got = self._call("is_SetColorMode", self._h, IS_GET_COLOR_MODE)
        if got != mode:
            raise UeyeError(f"colour mode reads {got}, requested {mode}")
        return True

    def set_master_gain_zero(self) -> int:
        rc = self._call("is_SetHardwareGain", self._h, 0, -1, -1, -1)  # status code for a set
        if rc != IS_SUCCESS:
            raise UeyeError(f"is_SetHardwareGain(0) returned {rc}")
        return self._call("is_SetHardwareGain", self._h, IS_GET_MASTER_GAIN, -1, -1, -1)

    def set_blacklevel_manual(self, offset: int, *, mode_settable: bool = True) -> dict[str, int]:
        """Auto black level off (only if the camera's caps allow it), then the offset. Rev 2: the UI-146xLE
        reports caps 2 (offset only); its auto black level stays on and the offset is added to it."""
        self.allowed_offset = offset
        if mode_settable:
            mode = C.c_int(IS_AUTO_BLACKLEVEL_OFF)
            self._call("is_Blacklevel", self._h, BL_SET_MODE, C.byref(mode), C.sizeof(mode))
        off = C.c_int(offset)
        self._call("is_Blacklevel", self._h, BL_SET_OFFSET, C.byref(off), C.sizeof(off))
        return {"mode": self._bl_int(BL_GET_MODE), "offset": self._bl_int(BL_GET_OFFSET)}

    def set_exposure(self, ms: float) -> float:
        v = C.c_double(ms)
        self._call("is_Exposure", self._h, EXPOSURE_SET, C.byref(v), C.sizeof(v))
        got = C.c_double()
        self._call("is_Exposure", self._h, EXPOSURE_GET, C.byref(got), C.sizeof(got))
        return got.value

    def reopen(self) -> None:
        """Restore check: drop the session (is_ExitCamera), then a fresh flag-free init."""
        self.close()
        self.connect()


def offset_target(default: int, rng: list[int]) -> int:
    lo, hi, inc = rng
    inc = max(1, inc)
    raw = default + OFFSET_FRACTION * (hi - lo)
    return int(min(hi, max(lo, lo + round((raw - lo) / inc) * inc)))


UEYE_VID, UEYE_PID = 0x1409, 0x1465


def ueye_present() -> bool:
    """Read-only probe: the uEye API file exists and exactly one 1409:1465 has a working driver (no camera opened)."""
    if sys.platform != "win32" or not DEFAULT_DLL.exists():
        return False
    pat = f"USB\\VID_{UEYE_VID:04X}&PID_{UEYE_PID:04X}\\*"
    cmd = ("Get-PnpDevice -PresentOnly -InstanceId '" + pat + "' -ErrorAction SilentlyContinue | "
           "% { (Get-PnpDeviceProperty -InstanceId $_.InstanceId -KeyName DEVPKEY_Device_ProblemCode).Data }")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
                             check=False, capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    codes = [c.strip() for c in out.splitlines() if c.strip()]
    return codes == ["0"]
