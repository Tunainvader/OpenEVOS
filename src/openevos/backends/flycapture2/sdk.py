"""FlyCapture2 C adapter (ctypes) for the Chameleon mono camera: shared by Camera Gates 1/2 (ADR-0033,
ADR-0036) and the app backend (ADR-0037). Lives under backends/ so the app never imports maintenance
tools (ADR-0011).

ctypes against `FlyCapture2_C_v140.dll` (FlyCapture 2.13.3.61). Struct layouts follow
`include/C/FlyCapture2Defs_C.h`; every output struct is over-allocated with a trailing pad so
a layout difference cannot overrun our memory (we read only leading fields).

Only these SDK functions are bound: fc2CreateContext, fc2DestroyContext, fc2GetNumOfCameras,
fc2GetCameraFromIndex, fc2Connect, fc2Disconnect, fc2GetCameraInfo, fc2GetLibraryVersion,
fc2GetVideoModeAndFrameRate, fc2GetFormat7Configuration, fc2GetFormat7Info, fc2GetPropertyInfo,
fc2GetProperty, fc2StartCapture, fc2RetrieveBuffer, fc2StopCapture, fc2CreateImage,
fc2DestroyImage, fc2ErrorToDescription. No Set*/Write*/memory-channel/firmware function is bound.
"""

from __future__ import annotations

import ctypes as C
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import numpy as np

DEFAULT_DLL = Path(r"C:\Program Files\Point Grey Research\FlyCapture2\bin64\vs2015\FlyCapture2_C_v140.dll")
MAX_STR = 512
PAD = 8192

BOUND = ("fc2CreateContext", "fc2DestroyContext", "fc2GetNumOfCameras", "fc2GetCameraFromIndex",
         "fc2Connect", "fc2Disconnect", "fc2GetCameraInfo", "fc2GetLibraryVersion",
         "fc2GetVideoModeAndFrameRate", "fc2GetFormat7Configuration", "fc2GetFormat7Info",
         "fc2GetPropertyInfo", "fc2GetProperty", "fc2StartCapture", "fc2RetrieveBuffer",
         "fc2StopCapture", "fc2CreateImage", "fc2DestroyImage", "fc2ErrorToDescription")

PIXEL_FORMATS = {0x80000000: ("MONO8", 8), 0x04000000: ("MONO16", 16), 0x00400000: ("RAW8", 8),
                 0x00200000: ("RAW16", 16), 0x00100000: ("MONO12", 12)}
PROPERTIES = {"brightness": 0, "auto_exposure": 1, "gamma": 6, "shutter": 12, "gain": 13,
              "frame_rate": 16, "temperature": 17}
VIDEOMODE_FORMAT7 = 23


class PGRGuid(C.Structure):
    _fields_ = [("value", C.c_uint * 4)]


class Version(C.Structure):
    _fields_ = [("major", C.c_uint), ("minor", C.c_uint), ("type", C.c_uint), ("build", C.c_uint)]


class CameraInfo(C.Structure):
    _fields_ = [("serialNumber", C.c_uint), ("interfaceType", C.c_int), ("driverType", C.c_int),
                ("isColorCamera", C.c_int), ("modelName", C.c_char * MAX_STR),
                ("vendorName", C.c_char * MAX_STR), ("sensorInfo", C.c_char * MAX_STR),
                ("sensorResolution", C.c_char * MAX_STR), ("driverName", C.c_char * MAX_STR),
                ("fw_ver", C.c_char * MAX_STR), ("fw_build", C.c_char * MAX_STR),
                ("_pad", C.c_ubyte * PAD)]


class Format7ImageSettings(C.Structure):
    _fields_ = [("mode", C.c_int), ("offsetX", C.c_uint), ("offsetY", C.c_uint), ("width", C.c_uint),
                ("height", C.c_uint), ("pixelFormat", C.c_uint), ("_pad", C.c_ubyte * PAD)]


class Format7Info(C.Structure):
    _fields_ = [("mode", C.c_int), ("maxWidth", C.c_uint), ("maxHeight", C.c_uint),
                ("offsetHStepSize", C.c_uint), ("offsetVStepSize", C.c_uint),
                ("imageHStepSize", C.c_uint), ("imageVStepSize", C.c_uint),
                ("pixelFormatBitField", C.c_uint), ("vendorPixelFormatBitField", C.c_uint),
                ("packetSize", C.c_uint), ("minPacketSize", C.c_uint), ("maxPacketSize", C.c_uint),
                ("percentage", C.c_float), ("_pad", C.c_ubyte * PAD)]


class PropertyInfo(C.Structure):
    _fields_ = [("type", C.c_int), ("present", C.c_int), ("autoSupported", C.c_int),
                ("manualSupported", C.c_int), ("onOffSupported", C.c_int), ("onePushSupported", C.c_int),
                ("absValSupported", C.c_int), ("readOutSupported", C.c_int), ("min", C.c_uint),
                ("max", C.c_uint), ("absMin", C.c_float), ("absMax", C.c_float),
                ("pUnits", C.c_char * MAX_STR), ("pUnitAbbr", C.c_char * MAX_STR), ("_pad", C.c_ubyte * PAD)]


class Property(C.Structure):
    _fields_ = [("type", C.c_int), ("present", C.c_int), ("absControl", C.c_int), ("onePush", C.c_int),
                ("onOff", C.c_int), ("autoManualMode", C.c_int), ("valueA", C.c_uint), ("valueB", C.c_uint),
                ("absValue", C.c_float), ("_pad", C.c_ubyte * PAD)]


class Image(C.Structure):
    _fields_ = [("rows", C.c_uint), ("cols", C.c_uint), ("stride", C.c_uint),
                ("pData", C.POINTER(C.c_ubyte)), ("dataSize", C.c_uint), ("receivedDataSize", C.c_uint),
                ("format", C.c_uint), ("bayerFormat", C.c_int), ("imageImpl", C.c_void_p),
                ("_pad", C.c_ubyte * PAD)]


class Fc2Error(RuntimeError):
    code: int | None = None


def _s(b: bytes) -> str:
    return b.decode("ascii", "replace").strip()


def image_to_array(img: Image) -> tuple[np.ndarray, str, int]:
    """Copy an fc2Image (rows/cols/stride) into a fresh array: (pixels, format name, bit depth)."""
    name, depth = PIXEL_FORMATS.get(img.format, (f"0x{img.format:08X}", 0))
    if depth == 0 or name == "MONO12":
        raise Fc2Error(f"unsupported pixel format {name}")
    bpp = 2 if depth > 8 else 1
    raw = np.ctypeslib.as_array(img.pData, shape=(img.rows * img.stride,)).copy()
    rows = raw.reshape(img.rows, img.stride)[:, : img.cols * bpp]
    # 16-bit data arrive big-endian (IIDC Y16): Verified on the CMLN-13S2M, Gate 2 run 2026-11-13 18:23 UTC.
    px = np.ascontiguousarray(rows).view(">u2" if bpp == 2 else np.uint8).reshape(img.rows, img.cols)
    if bpp == 2:
        px = px.astype(np.uint16)  # native byte order
    return px, name, depth


class FlyCapture2GateBackend:
    """Implements `camera_gate1.CameraGateBackend` with the SDK's read-only calls."""

    def __init__(self, dll_path: Path = DEFAULT_DLL, loader=None):
        self.dll_path = Path(dll_path)
        self._loader = loader
        self._lib = None
        self._ctx = C.c_void_p()
        self._connected = False
        self._capturing = False

    # -- loading -------------------------------------------------------------------------
    def _load(self):
        if self._lib is None:
            if self._loader is not None:
                self._lib = self._loader(self.dll_path)
            else:
                if hasattr(os, "add_dll_directory"):
                    os.add_dll_directory(str(self.dll_path.parent))
                self._lib = C.CDLL(str(self.dll_path))
            self._lib.fc2ErrorToDescription.restype = C.c_char_p
        return self._lib

    ALLOWED: tuple[str, ...] = BOUND

    def _call(self, name: str, *args) -> None:
        if name not in self.ALLOWED:
            raise Fc2Error(f"{name} is not in the reviewed read-only set")
        lib = self._load()
        rc = getattr(lib, name)(*args)
        if rc != 0:
            desc = lib.fc2ErrorToDescription(rc)
            err = Fc2Error(f"{name} returned {rc}: {_s(desc) if desc else ''}")
            err.code = rc
            raise err

    def sdk_available(self) -> bool:
        if self._loader is None and not self.dll_path.exists():
            return False
        try:
            self._load()
        except OSError:
            return False
        return True

    def count_cameras(self) -> int:
        """Bus enumeration on a temporary context; does not connect to any camera."""
        ctx, n = C.c_void_p(), C.c_uint(0)
        self._call("fc2CreateContext", C.byref(ctx))
        try:
            self._call("fc2GetNumOfCameras", ctx, C.byref(n))
        finally:
            self._call("fc2DestroyContext", ctx)
        return n.value

    # -- gate steps ------------------------------------------------------------------------
    def connect(self) -> None:
        self._call("fc2CreateContext", C.byref(self._ctx))
        guid = PGRGuid()
        self._call("fc2GetCameraFromIndex", self._ctx, 0, C.byref(guid))
        self._call("fc2Connect", self._ctx, C.byref(guid))
        self._connected = True

    def identity(self) -> dict[str, Any]:
        info, ver = CameraInfo(), Version()
        self._call("fc2GetCameraInfo", self._ctx, C.byref(info))
        self._call("fc2GetLibraryVersion", C.byref(ver))
        return {"serial": info.serialNumber, "model": _s(info.modelName), "vendor": _s(info.vendorName),
                "sensor": _s(info.sensorInfo), "resolution": _s(info.sensorResolution),
                "fw_version": _s(info.fw_ver), "fw_build": _s(info.fw_build),
                "driver": _s(info.driverName), "is_color": bool(info.isColorCamera),
                "interface_type": info.interfaceType, "driver_type": info.driverType,
                "library_version": f"{ver.major}.{ver.minor}.{ver.type}.{ver.build}"}

    def modes(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        vm, fr = C.c_int(), C.c_int()
        self._call("fc2GetVideoModeAndFrameRate", self._ctx, C.byref(vm), C.byref(fr))
        out["video_mode"], out["frame_rate_enum"] = vm.value, fr.value
        if vm.value == VIDEOMODE_FORMAT7:
            s, pkt, pct = Format7ImageSettings(), C.c_uint(), C.c_float()
            self._call("fc2GetFormat7Configuration", self._ctx, C.byref(s), C.byref(pkt), C.byref(pct))
            out["format7_current"] = {"mode": s.mode, "offset": [s.offsetX, s.offsetY],
                                      "size": [s.width, s.height],
                                      "pixel_format": PIXEL_FORMATS.get(s.pixelFormat, (hex(s.pixelFormat),))[0],
                                      "packet_size": pkt.value}
        f7 = []
        for mode in range(8):
            info, ok = Format7Info(), C.c_int(0)
            info.mode = mode
            try:
                self._call("fc2GetFormat7Info", self._ctx, C.byref(info), C.byref(ok))
            except Fc2Error:
                continue
            if ok.value:
                f7.append({"mode": mode, "max": [info.maxWidth, info.maxHeight],
                           "pixel_formats": [n for b, (n, _) in PIXEL_FORMATS.items()
                                             if info.pixelFormatBitField & b]})
        out["format7_modes"] = f7
        props = {}
        for name, t in PROPERTIES.items():
            pi, pv = PropertyInfo(), Property()
            pi.type = pv.type = t
            try:
                self._call("fc2GetPropertyInfo", self._ctx, C.byref(pi))
                if not pi.present:
                    props[name] = {"present": False}
                    continue
                entry = {"present": True, "range": [pi.min, pi.max], "abs_range": [pi.absMin, pi.absMax],
                         "units": _s(pi.pUnitAbbr), "auto_supported": bool(pi.autoSupported)}
                if pi.readOutSupported:
                    self._call("fc2GetProperty", self._ctx, C.byref(pv))
                    entry.update(value=pv.valueA, abs_value=pv.absValue, auto=bool(pv.autoManualMode),
                                 on=bool(pv.onOff))
                props[name] = entry
            except Fc2Error as exc:
                props[name] = {"error": str(exc)}
        out["properties"] = props
        return out

    def grab_one(self, timeout_s: float) -> tuple[np.ndarray, int]:
        img = Image()
        self._call("fc2CreateImage", C.byref(img))
        try:
            self._call("fc2StartCapture", self._ctx)
            self._capturing = True
            box: dict[str, Any] = {}

            def worker():
                try:
                    self._call("fc2RetrieveBuffer", self._ctx, C.byref(img))
                    box["ok"] = True
                except Exception as exc:  # noqa: BLE001
                    box["err"] = exc

            t = threading.Thread(target=worker, daemon=True)
            t.start()
            t.join(timeout_s)
            if t.is_alive():
                self._stop_capture()  # aborts the blocked RetrieveBuffer
                t.join(2.0)
                raise Fc2Error(f"no frame within {timeout_s} s")
            if "err" in box:
                raise box["err"]
            px, _name, depth = image_to_array(img)
            self._stop_capture()
            return px, depth
        finally:
            self._stop_capture()
            try:
                self._call("fc2DestroyImage", C.byref(img))
            except Fc2Error:
                pass

    def _stop_capture(self) -> None:
        if self._capturing:
            self._capturing = False
            self._call("fc2StopCapture", self._ctx)

    def close(self) -> None:
        try:
            self._stop_capture()
        finally:
            try:
                if self._connected:
                    self._connected = False
                    self._call("fc2Disconnect", self._ctx)
            finally:
                if self._ctx.value:
                    self._call("fc2DestroyContext", self._ctx)
                    self._ctx = C.c_void_p()


# -- Gate 2 settings path (ADR-0036 spec Â§4), shared with the app backend (ADR-0037) ------------------------------------------------------------------  # noqa: E501
F7_MODE, F7_WIDTH, F7_HEIGHT, F7_FORMAT = 0, 1296, 964, 0x04000000  # MONO16
BRIGHTNESS_PCT = 2.0
PACKET_FRACTION = 0.75  # rev 2: below the 3,904 B maximum, to leave USB 2.0 isochronous headroom
MAX_SKIPPED_PER_EXPOSURE = 3  # incomplete frames skipped per grab_series call
FC2_IMAGE_CONSISTENCY_ERROR = 41  # incomplete frame; rev 2 skips it (a read, nothing is sent)
SETTABLE = {"auto_exposure", "shutter", "gain", "brightness", "frame_rate"}
COMPARED = ("brightness", "auto_exposure", "gamma", "shutter", "gain", "frame_rate")
TOL = {"shutter": lambda v: 0.02 * v + 0.02, "gain": lambda v: 0.1, "brightness": lambda v: 0.05}

SETTER_BOUND = BOUND + ("fc2GetMemoryChannel", "fc2GetMemoryChannelInfo", "fc2ValidateFormat7Settings",
                           "fc2SetFormat7ConfigurationPacket", "fc2SetProperty", "fc2SetVideoModeAndFrameRate")


class Format7PacketInfo(C.Structure):
    _fields_ = [("recommendedBytesPerPacket", C.c_uint), ("maxBytesPerPacket", C.c_uint),
                ("unitBytesPerPacket", C.c_uint), ("_pad", C.c_ubyte * PAD)]


class Gate2Error(RuntimeError):
    pass


class FlyCapture2SettingsBackend(FlyCapture2GateBackend):
    """Gate 1 backend plus the four reviewed volatile-register setters."""

    ALLOWED = SETTER_BOUND

    # -- reads -------------------------------------------------------------------------
    def get_prop(self, name: str) -> dict[str, Any]:
        p = Property()
        p.type = PROPERTIES[name]
        self._call("fc2GetProperty", self._ctx, C.byref(p))
        return {"present": bool(p.present), "abs_control": bool(p.absControl), "one_push": bool(p.onePush),
                "on": bool(p.onOff), "auto": bool(p.autoManualMode), "value": p.valueA, "value_b": p.valueB,
                "abs_value": float(p.absValue)}

    def snapshot(self) -> dict[str, Any]:
        vm, fr, ch, nch = C.c_int(), C.c_int(), C.c_uint(), C.c_uint()
        self._call("fc2GetVideoModeAndFrameRate", self._ctx, C.byref(vm), C.byref(fr))
        snap: dict[str, Any] = {"video_mode": vm.value, "frame_rate_enum": fr.value}
        try:
            self._call("fc2GetMemoryChannelInfo", self._ctx, C.byref(nch))
            self._call("fc2GetMemoryChannel", self._ctx, C.byref(ch))
            snap["memory_channels"], snap["memory_channel"] = nch.value, ch.value
        except Fc2Error as exc:
            snap["memory_channel_error"] = str(exc)
        snap["properties"] = {n: self.get_prop(n) for n in COMPARED}
        return snap

    # -- writes (volatile registers only) ------------------------------------------------
    def set_format7_mono16_full(self) -> dict[str, Any]:
        s, ok, pkt = Format7ImageSettings(), C.c_int(0), Format7PacketInfo()
        s.mode, s.offsetX, s.offsetY, s.width, s.height, s.pixelFormat = F7_MODE, 0, 0, F7_WIDTH, F7_HEIGHT, F7_FORMAT
        self._call("fc2ValidateFormat7Settings", self._ctx, C.byref(s), C.byref(ok), C.byref(pkt))
        if not ok.value:
            raise Gate2Error("Format7 mode 0 1296x964 MONO16 not valid on this camera")
        unit = pkt.unitBytesPerPacket or 4
        size = max(unit, int(pkt.maxBytesPerPacket * PACKET_FRACTION) // unit * unit)
        size = min(size, pkt.recommendedBytesPerPacket or size)
        self._call("fc2SetFormat7ConfigurationPacket", self._ctx, C.byref(s), C.c_uint(size))
        return {"packet_recommended": pkt.recommendedBytesPerPacket, "packet_max": pkt.maxBytesPerPacket,
                "packet_unit": pkt.unitBytesPerPacket, "packet_used": size}

    def set_prop(self, name: str, *, on: bool, auto: bool, abs_value: float | None = None,
                 raw_value: int | None = None) -> None:
        if name not in SETTABLE:
            raise Gate2Error(f"property {name} is not settable in Gate 2")
        p = Property()
        p.type = PROPERTIES[name]
        self._call("fc2GetProperty", self._ctx, C.byref(p))
        p.onOff, p.autoManualMode, p.onePush = int(on), int(auto), 0
        if abs_value is not None:
            p.absControl, p.absValue = 1, abs_value
        elif raw_value is not None:
            p.absControl, p.valueA = 0, raw_value
        self._call("fc2SetProperty", self._ctx, C.byref(p))

    def set_video_mode(self, video_mode: int, frame_rate: int) -> None:
        self._call("fc2SetVideoModeAndFrameRate", self._ctx, C.c_int(video_mode), C.c_int(frame_rate))

    # -- capture -------------------------------------------------------------------------
    def grab_series(self, n_discard: int, n_keep: int, timeout_s: float) -> list[np.ndarray]:
        img = Image()
        self._call("fc2CreateImage", C.byref(img))
        frames: list[np.ndarray] = []
        self.skipped = 0
        try:
            self._call("fc2StartCapture", self._ctx)
            self._capturing = True
            got = 0
            while got < n_discard + n_keep:
                i = got
                box: dict[str, Any] = {}

                def worker(box=box):
                    try:
                        self._call("fc2RetrieveBuffer", self._ctx, C.byref(img))
                        box["ok"] = True
                    except Exception as exc:  # noqa: BLE001
                        box["err"] = exc

                t = threading.Thread(target=worker, daemon=True)
                t.start()
                t.join(timeout_s)
                if t.is_alive():
                    self._stop_capture()
                    t.join(2.0)
                    raise Gate2Error(f"no frame within {timeout_s} s")
                if "err" in box:
                    err = box["err"]
                    if getattr(err, "code", None) == FC2_IMAGE_CONSISTENCY_ERROR and self.skipped < MAX_SKIPPED_PER_EXPOSURE:  # noqa: E501
                        self.skipped += 1
                        continue
                    raise err
                px, _name, _depth = image_to_array(img)
                got += 1
                if i >= n_discard:
                    frames.append(px)
        finally:
            self._stop_capture()
            try:
                self._call("fc2DestroyImage", C.byref(img))
            except Fc2Error:
                pass
        return frames


def compare_state(ref: dict[str, Any], cur: dict[str, Any]) -> list[str]:
    """Differences in video mode, frame rate, flags and (for non-auto properties) values."""
    diffs = []
    for k in ("video_mode", "frame_rate_enum"):
        if ref.get(k) != cur.get(k):
            diffs.append(f"{k}: {ref.get(k)} -> {cur.get(k)}")
    for name in COMPARED:
        r, c = ref.get("properties", {}).get(name), cur.get("properties", {}).get(name)
        if not r or not c or not r.get("present", True):
            continue
        for flag in ("on", "auto"):
            if flag in r and r[flag] != c.get(flag):
                diffs.append(f"{name}.{flag}: {r[flag]} -> {c.get(flag)}")
        if not r.get("auto") and "value" in r and r["value"] != c.get("value"):
            diffs.append(f"{name}.value: {r['value']} -> {c.get('value')}")
    return diffs


def restore_state(b: FlyCapture2SettingsBackend, snap: dict[str, Any]) -> None:
    b.set_video_mode(snap["video_mode"], snap["frame_rate_enum"])
    for name in ("auto_exposure", "brightness", "gain", "shutter", "frame_rate"):
        p = snap["properties"].get(name)
        if p and p["present"]:
            b.set_prop(name, on=p["on"], auto=p["auto"], raw_value=p["value"])


CHAMELEON_VID, CHAMELEON_PID = 0x1E10, 0x2005


def chameleon_present() -> bool:
    """Read-only probe: the SDK file exists and exactly one 1E10:2005 has a working driver (no camera opened)."""
    if sys.platform != "win32" or not DEFAULT_DLL.exists():
        return False
    pat = f"USB\\VID_{CHAMELEON_VID:04X}&PID_{CHAMELEON_PID:04X}\\*"
    cmd = ("Get-PnpDevice -PresentOnly -InstanceId '" + pat + "' -ErrorAction SilentlyContinue | "
           "% { (Get-PnpDeviceProperty -InstanceId $_.InstanceId -KeyName DEVPKEY_Device_ProblemCode).Data }")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
                             check=False, capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    codes = [c.strip() for c in out.splitlines() if c.strip()]
    return codes == ["0"]
