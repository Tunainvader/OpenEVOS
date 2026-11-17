"""Physical colour camera for the command core: uEye UI-146xLE-C through the classic uEye API (ADR-0039).

Same backend contract as the simulated rig camera and the mono Chameleon backend (ADR-0037):
DeviceBackend methods, `camera.set_exposure` / `camera.set_gain`, and `start_stream` / `stop_stream`
pushing (H, W, 3) RGB uint8 frames into the core's FrameTransport, `synthetic=False`.

Camera envelope (inside what colour Gates 1 and 2 proved, ADR-0033/0038):
* connect: flag-free `is_InitCamera` (driver defaults: BGR8_PACKED, 2048x1536, 24 MHz), refuse unless the
  model is UI146xLE and the mode/area are those defaults; then master gain 0 and a default exposure, each
  read back. The colour mode is never changed: the IDS library demosaics and colour-converts BGR8, so
  the Bayer order is its business. Black level stays at its default (auto, offset 0).
* commands: exposure within the range the camera reports (0.10-199.7 ms at 24 MHz); gain only 1x
  (0 dB, the only gain Gate 2 proved). Read back.
* disconnect: free the host buffer, `is_ExitCamera`. The session's settings are dropped; Gate 2 verified
  that a fresh init returns the defaults.
"""

from __future__ import annotations

import ctypes as C
import itertools
import threading
from collections.abc import Callable

import numpy as np

from openevos.backends.ueye import sdk as ue
from openevos.core.frame_transport import FrameMetadata, FrameTransport
from openevos.core.models import (
    Activity,
    BackendKind,
    Command,
    Connection,
    DeviceKind,
    FirmwareMode,
    Health,
    Position,
    SetCameraExposure,
    SetCameraGain,
)
from openevos.hal.backend import ExecutionContext, Reporter
from openevos.safety.capabilities import (
    AxisLimits,
    CapabilitySource,
    DeviceCapabilities,
    ParameterRange,
)

EVIDENCE = ("camera_gate1_spec.md#4f", "camera_gate2_colour_settings_spec.md#6b")
MODEL_TAG = "UI146xLE"
WIDTH, HEIGHT = 2048, 1536
BGR8 = 1
DEFAULT_EXPOSURE_MS = 20.0
FREEZE_TIMEOUT_S = 5.0
MAX_CONSECUTIVE_SKIPS = 5
EXPOSURE_TOL_MS = 0.11  # one 0.10 ms step at 24 MHz
STALE_FRAMES_AFTER_EXPOSURE = 1
STALE_FRAMES_AFTER_LIGHT = 1  # LED-change check 2026-11-15: first snap after ON was 61 % lit


class UeyeAppSdk(ue.UeyeGateBackend):
    """Gate 1 read-only set plus the two setters the app needs: exposure inside the range read from the
    camera, and master gain 0 (R/G/B untouched). Nothing else is settable."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.exposure_range: tuple[float, float] | None = None

    def _allowed(self, name: str, args: tuple) -> bool | None:
        if name == "is_Exposure" and ue._arg(args, 1) == ue.EXPOSURE_SET:
            if self.exposure_range is None:
                return False
            v = ue._deref(args[2])
            return self.exposure_range[0] - 1e-9 <= v <= self.exposure_range[1] + 1e-9
        if name == "is_SetHardwareGain" and ue._arg(args, 1) < 0x8000:
            return tuple(ue._arg(args, i) for i in (1, 2, 3, 4)) == (0, -1, -1, -1)
        return super()._allowed(name, args)

    def get_exposure(self) -> float:
        v = C.c_double()
        self._call("is_Exposure", self._h, ue.EXPOSURE_GET, C.byref(v), C.sizeof(v))
        return v.value

    def set_exposure(self, ms: float) -> float:
        v = C.c_double(ms)
        self._call("is_Exposure", self._h, ue.EXPOSURE_SET, C.byref(v), C.sizeof(v))
        return self.get_exposure()

    def read_exposure_range(self) -> tuple[float, float, float]:
        rng = (C.c_double * 3)()
        self._call("is_Exposure", self._h, ue.EXPOSURE_GET_RANGE, rng, C.sizeof(rng))
        return rng[0], rng[1], rng[2]

    def set_master_gain_zero(self) -> int:
        rc = self._call("is_SetHardwareGain", self._h, 0, -1, -1, -1)
        if rc != ue.IS_SUCCESS:
            raise ue.UeyeError(f"is_SetHardwareGain(0) returned {rc}")
        return self._call("is_SetHardwareGain", self._h, ue.IS_GET_MASTER_GAIN, -1, -1, -1)

    def color_mode(self) -> int:
        return self._call("is_SetColorMode", self._h, ue.IS_GET_COLOR_MODE)

    def aoi(self) -> tuple[int, int]:
        r = ue.IS_RECT()
        self._call("is_AOI", self._h, ue.IS_AOI_IMAGE_GET_AOI, C.byref(r), C.sizeof(r))
        return r.s32Width, r.s32Height

    def alloc(self, w: int, h: int, bpp: int) -> int:
        mem, mid = C.c_void_p(), C.c_int()
        self._call("is_AllocImageMem", self._h, w, h, bpp, C.byref(mem), C.byref(mid))
        self._mem = (mem, mid)
        self._call("is_SetImageMem", self._h, mem, mid)
        pitch = C.c_int()
        self._call("is_GetImageMemPitch", self._h, C.byref(pitch))
        return pitch.value

    def freeze(self, timeout_s: float) -> C.c_void_p:
        self._call("is_FreezeVideo", self._h, max(10, min(32768, int(timeout_s * 100))))
        mem, _ = self._mem
        return mem


class UeyeColourCamera:
    kind = DeviceKind.CAMERA
    backend_kind = BackendKind.PHYSICAL

    def __init__(self, device_id: str, transport: FrameTransport, *, sdk: UeyeAppSdk | None = None,
                 channel_fn: Callable[[], str | None] | None = None,
                 extra_fn: Callable[[], tuple[tuple[str, str], ...]] | None = None):
        self.device_id, self.transport = device_id, transport
        self.physical_id = "ueye:1409:1465"
        self._sdk = sdk or UeyeAppSdk()
        self._channel_fn, self._extra_fn = channel_fn, extra_fn
        self.exposure_ms, self.gain_db = DEFAULT_EXPOSURE_MS, 0.0
        self.exposure_range_ms: tuple[float, float] | None = None
        self.identity: dict = {}
        self._pitch = 0
        self._stale = 0  # frames still to drop after an exposure change
        self.stale_dropped = 0
        self._connected = False
        self._io = threading.RLock()  # every SDK call
        self._lock = threading.RLock()
        self._seq = itertools.count(1)
        self._report: Reporter | None = None
        self._stream: dict | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.frames_produced = 0
        self.skipped_grabs = 0

    # ------------------------------------------------------------------ DeviceBackend
    def attach(self, reporter: Reporter) -> None:
        self._report = reporter

    def _obs(self, **fields) -> None:
        if self._report is not None:
            self._report(seq=next(self._seq), **fields)

    def connect(self) -> None:
        self._obs(connection=Connection.CONNECTING, activity=Activity.INITIALIZING, reason="opening uEye (IDS uEye API)")
        s = self._sdk
        try:
            with self._io:
                if not s.sdk_available():
                    raise RuntimeError("uEye API (ueye_api_64.dll) not available")
                n = s.count_cameras()
                if n != 1:
                    raise RuntimeError(f"uEye API sees {n} cameras, expected 1")
                s.connect()
                self.identity = s.identity()
                if MODEL_TAG not in (self.identity.get("model") or ""):
                    raise RuntimeError(f"unexpected camera model {self.identity.get('model')!r}")
                mode, (w, h) = s.color_mode(), s.aoi()
                if mode != BGR8 or (w, h) != (WIDTH, HEIGHT):
                    raise RuntimeError(f"camera not in its driver defaults (mode {mode}, {w}x{h}); not changed")
                lo, hi, _inc = s.read_exposure_range()
                s.exposure_range = self.exposure_range_ms = (lo, hi)
                gain = s.set_master_gain_zero()
                if gain != 0:
                    raise RuntimeError(f"master gain reads {gain}, requested 0")
                self._set_exposure(DEFAULT_EXPOSURE_MS)
                self._pitch = s.alloc(WIDTH, HEIGHT, 24)
            self._connected = True
        except Exception as exc:  # noqa: BLE001 - report as a fault; exit drops anything set
            self._safe_close()
            self._obs(connection=Connection.DISCONNECTED, health=Health.FAULT, activity=Activity.UNKNOWN,
                      reason="camera connect failed", fault=str(exc))
            return
        # ADR-0009: the uEye API identified the camera running its driver-loaded application image
        self._obs(connection=Connection.CONNECTED, health=Health.NORMAL, activity=Activity.IDLE,
                  firmware_mode=FirmwareMode.APPLICATION,
                  attributes={"model": self.identity.get("model"), "exposure_ms": self.exposure_ms, "gain_db": 0.0},
                  reason="uEye UI-146xLE connected (2048x1536 colour, manual exposure)")

    def _set_exposure(self, ms: float) -> None:
        got = self._sdk.set_exposure(ms)
        # Freerun pipelining (ADR-0039 "Exposure lag"): the sensor runs continuously and is_FreezeVideo
        # returns the next frame to finish, which may have started exposing before is_Exposure was set.
        # The uEye API has no wait/flush for this, so the stream drops that one frame. Set before the
        # readback check: a failed check may still have changed the exposure.
        self._stale = STALE_FRAMES_AFTER_EXPOSURE
        if abs(got - ms) > EXPOSURE_TOL_MS:
            raise RuntimeError(f"exposure reads {got:.4g} ms, requested {ms} ms")
        self.exposure_ms = got

    def invalidate_frames(self, reason: str = "") -> None:
        """Drop the next frame: something outside the camera changed (the light), and the frame in flight
        started exposing before it (ADR-0039, "LED-change lag"). Waits for a freeze in progress: that frame
        still reaches live view; the next freeze (for a snap, its first) is the one dropped."""
        with self._io:
            self._stale = max(self._stale, STALE_FRAMES_AFTER_LIGHT)

    def disconnect(self) -> None:
        self.stop_stream("disconnect")
        self._safe_close()
        self._obs(connection=Connection.DISCONNECTED, activity=Activity.UNKNOWN,
                  reason="camera closed (session settings dropped by is_ExitCamera)")

    def simulate_unexpected_disconnect(self) -> None:
        self._obs(reason="'simulate disconnect' ignored: this is the physical camera")

    def halt(self) -> None:
        self.stop_stream("halt")

    def _safe_close(self) -> None:
        with self._io:
            self._connected = False
            try:
                self._sdk.close()
            except Exception:  # noqa: BLE001, S110 - best effort
                pass

    def start(self, command: Command, ctx: ExecutionContext) -> None:
        if not isinstance(command, (SetCameraExposure, SetCameraGain)):
            ctx.fail(f"camera cannot execute {command.kind}", "UNSUPPORTED")
            return
        if not self._connected:
            ctx.fail("camera not connected", "NOT_CONNECTED")
            return
        if isinstance(command, SetCameraGain) and abs(command.gain_db) > 1e-6:
            ctx.fail("the colour camera runs at gain 1x only (ADR-0039)", "UNSUPPORTED")
            return
        ctx.mark_started()
        try:
            if isinstance(command, SetCameraExposure):
                with self._io:
                    self._set_exposure(command.exposure_ms)
        except Exception as exc:  # noqa: BLE001
            ctx.fail(str(exc), "DEVICE_ERROR")
            return
        ctx.complete(None)

    def verify_result(self, command: Command) -> tuple[bool, str]:
        if isinstance(command, SetCameraExposure) and abs(self.exposure_ms - command.exposure_ms) > EXPOSURE_TOL_MS:
            return False, f"exposure reads {self.exposure_ms:.4g} ms, commanded {command.exposure_ms} ms"
        if isinstance(command, SetCameraGain) and abs(command.gain_db) > 1e-6:
            return False, "gain is fixed at 1x"
        return True, ""

    def cancel(self, command_id: str) -> None:
        return None

    def read_position(self) -> Position | None:
        return None

    def recover(self) -> None:
        self.stop_stream("recover")
        self._obs(health=Health.NORMAL if self._connected else Health.FAULT, activity=Activity.IDLE, reason="recovered")

    def capabilities(self) -> DeviceCapabilities:
        exp = self.exposure_range_ms or (0.1, 199.0)  # Gate 1/2 range until connect reads it
        return DeviceCapabilities(
            device_id=self.device_id, backend_kind=BackendKind.PHYSICAL, source=CapabilitySource.VERIFIED,
            supported_commands=frozenset({"camera.set_exposure", "camera.set_gain"}),
            ranges=(ParameterRange(name="exposure_ms", limits=AxisLimits(minimum=exp[0], maximum=exp[1], unit="ms")),
                    # gain is fixed at 1x; AxisLimits needs min < max, so a sliver above 0 dB (start() refuses it)
                    ParameterRange(name="gain_db", limits=AxisLimits(minimum=0.0, maximum=0.001, unit="dB"))),
            evidence_ids=EVIDENCE,
            note="uEye UI-146xLE-C via the uEye API: 2048x1536 BGR8 (IDS demosaic), manual exposure, gain 1x, "
                 "session-RAM settings (ADR-0038/0039)")

    # ------------------------------------------------------------------ streaming
    def start_stream(self, *, acquisition_id: str | None = None, n_frames: int | None = None,
                     on_finished: Callable[[str], None] | None = None) -> None:
        with self._lock:
            if self._stream is not None:
                raise RuntimeError("stream already running")
            if not self._connected:
                raise RuntimeError("camera not connected")
            self._stream = {"acq": acquisition_id, "n": n_frames, "i": 0, "done": on_finished}
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name=f"camera:{self.device_id}", daemon=True)
            self._thread.start()
        self._obs(activity=Activity.ACQUIRING, reason=f"streaming (acquisition={acquisition_id})")

    def stop_stream(self, reason: str = "stopped") -> None:
        with self._lock:
            s, self._stream = self._stream, None
            if s is None:
                return
            self._stop.set()
            t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(FREEZE_TIMEOUT_S + 1.0)  # at most one frame in flight
        self._thread = None
        self._obs(activity=Activity.IDLE, reason=f"stream stopped: {reason}")
        if s["done"]:
            s["done"](reason)

    @property
    def streaming(self) -> bool:
        return self._stream is not None

    @property
    def current_acquisition(self) -> str | None:
        s = self._stream
        return s["acq"] if s is not None else None

    def _loop(self) -> None:
        bad = 0
        while not self._stop.is_set():
            try:
                with self._io:
                    if not self._connected:
                        return
                    mem = self._sdk.freeze(FREEZE_TIMEOUT_S)
                    if self._stale > 0:  # exposed before the last exposure change
                        self._stale -= 1
                        self.stale_dropped += 1
                        continue
                    buf = C.string_at(mem, self._pitch * HEIGHT)
                    exposure = self.exposure_ms  # under the same lock as the freeze: the exposure it was taken at
            except ue.UeyeError as exc:
                if self._stop.is_set():
                    return
                if exc.code in ue.GRAB_SKIP_CODES and bad < MAX_CONSECUTIVE_SKIPS:
                    bad += 1
                    self.skipped_grabs += 1
                    continue
                self._fail_stream(f"frame grab failed: {exc}")
                return
            bad = 0
            px, _bits = ue.frame_from_buffer(buf, WIDTH, HEIGHT, self._pitch, BGR8)
            self._deliver(px, exposure)

    def _deliver(self, px: np.ndarray, exposure_ms: float) -> None:
        with self._lock:
            s = self._stream
            if s is None:
                return
            idx = s["i"]
            s["i"] += 1
        extra = (("gain_db", "0.00"), ("camera_model", self.identity.get("model") or ""),
                 ("color_pipeline", "uEye BGR8 (IDS demosaic)"), ("grabs_skipped", str(self.skipped_grabs)))
        if self._extra_fn is not None:
            extra += self._extra_fn()
        md = FrameMetadata(device_id=self.device_id, frame_index=idx, synthetic=False, acquisition_id=s["acq"],
                           channel=self._channel_fn() if self._channel_fn else None, exposure_ms=exposure_ms,
                           extra=extra)
        accepted = self.transport.push(px, md, owned=True)
        self.frames_produced += 1
        if not accepted:
            threading.Thread(target=self.stop_stream, args=(f"frame {idx} refused by storage path",), daemon=True).start()
            return
        if s["n"] is not None and s["i"] >= s["n"]:
            self._stop.set()
            threading.Thread(target=self.stop_stream, args=("requested frame count reached",), daemon=True).start()

    def _fail_stream(self, message: str) -> None:
        self._obs(health=Health.FAULT, reason="camera stream failed", fault=message)
        threading.Thread(target=self.stop_stream, args=(message,), daemon=True).start()
