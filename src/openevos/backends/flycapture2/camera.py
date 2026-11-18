"""Physical mono camera for the command core: Chameleon CMLN-13S2M through FlyCapture2 (ADR-0037).

Same backend contract as the simulated rig camera (`backends/rig/camera.py`): DeviceBackend methods,
`camera.set_exposure` / `camera.set_gain` commands, and `start_stream` / `stop_stream` pushing frames
into the core's FrameTransport. Frames carry `synthetic=False`.

What it does to the camera, and nothing else (the Gate 2 envelope, ADR-0036):
* connect: SDK connect, identity, snapshot of the power-up state, then the operating settings
  Format7 mode 0 1296x964 MONO16 at 75 % packet size, auto exposure off, manual gain and shutter,
  brightness 2 %. Every value is read back.
* commands: manual shutter (ms) and gain (dB) within the ranges read from the camera; read back.
* disconnect (and halt-on-close): stop capture, restore the power-up snapshot, verify, disconnect.
* Volatile registers only: the adapter binds no memory-channel, register, trigger or firmware call.

Pixels: the camera sends 12-bit data left-aligned in 16 bits, big-endian (Gate 2). Frames are delivered
as uint16 with the 4 padding bits shifted out (0..4095), matching the profile's 12-bit native depth.
"""

from __future__ import annotations

import ctypes as C
import itertools
import threading
import time
from collections.abc import Callable

import numpy as np

from openevos.backends.flycapture2 import sdk as fc
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

EVIDENCE = ("camera_gate1_spec.md#4b", "camera_gate2_mono_settings_spec.md#6c")
DEFAULT_EXPOSURE_MS = 20.0
DEFAULT_GAIN_DB = 0.0
RETRIEVE_TIMEOUT_S = 5.0
MAX_CONSECUTIVE_INCOMPLETE = 5
#: a retrieval error that is not a stop: restart capture in place (stop, short pause, start) up to this many times per
#: window before declaring the stream failed (operator, 2026-11-18: "again mono camera fault"; live view froze until
#: the app was restarted). Only fc2StopCapture / fc2StartCapture, the calls start_stream / stop_stream already make.
MAX_STREAM_RESTARTS = 3
RESTART_WINDOW_S = 60.0
RESTART_PAUSE_S = 0.3
BIT_SHIFT = 4  # 12-bit data left-aligned in 16 bits
STALE_FRAMES_AFTER_LIGHT = 1  # lag check 2026-11-16: first snap after LED ON was dark, after OFF part-lit


class FlyCapture2MonoCamera:
    kind = DeviceKind.CAMERA
    backend_kind = BackendKind.PHYSICAL

    def __init__(self, device_id: str, transport: FrameTransport, *,
                 sdk: fc.FlyCapture2SettingsBackend | None = None, channel_fn: Callable[[], str | None] | None = None,
                 extra_fn: Callable[[], tuple[tuple[str, str], ...]] | None = None):
        self.device_id, self.transport = device_id, transport
        self.physical_id = "flycapture2:1E10:2005"
        self._sdk = sdk or fc.FlyCapture2SettingsBackend()
        self._channel_fn, self._extra_fn = channel_fn, extra_fn
        self._restarts: list[float] = []  # monotonic times of in-place capture restarts (last RESTART_WINDOW_S)
        self._degraded = False
        self.stream_restarts = 0
        self.width, self.height = fc.F7_WIDTH, fc.F7_HEIGHT
        self.exposure_ms, self.gain_db = DEFAULT_EXPOSURE_MS, DEFAULT_GAIN_DB
        self.exposure_range_ms: tuple[float, float] | None = None
        self.gain_range_db: tuple[float, float] | None = None
        self.identity: dict = {}
        self._snapshot: dict | None = None
        self._connected = False
        self._io = threading.RLock()  # every SDK call
        self._lock = threading.RLock()
        self._seq = itertools.count(1)
        self._report: Reporter | None = None
        self._stream: dict | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.frames_produced = 0
        self.incomplete_frames = 0
        self._stale = 0  # frames still to drop after a light change
        self.stale_dropped = 0
        self.restore_diffs: list[str] | None = None

    # ------------------------------------------------------------------ DeviceBackend
    def attach(self, reporter: Reporter) -> None:
        self._report = reporter

    def _obs(self, **fields) -> None:
        if self._report is not None:
            self._report(seq=next(self._seq), **fields)

    def connect(self) -> None:
        self._obs(connection=Connection.CONNECTING, activity=Activity.INITIALIZING, reason="opening Chameleon (FlyCapture2)")
        try:
            with self._io:
                if not self._sdk.sdk_available():
                    raise RuntimeError("FlyCapture2 SDK not available")
                n = self._sdk.count_cameras()
                if n != 1:
                    raise RuntimeError(f"FlyCapture2 sees {n} cameras, expected 1")
                self._sdk.connect()
                self.identity = self._sdk.identity()
                if "CMLN-13S2M" not in self.identity.get("model", ""):
                    raise RuntimeError(f"unexpected camera model {self.identity.get('model')!r}")
                self._snapshot = self._sdk.snapshot()
                self._apply_operating_settings()
            self._connected = True
        except Exception as exc:  # noqa: BLE001 - report as a fault, leave the camera as found
            self._safe_close()
            self._obs(connection=Connection.DISCONNECTED, health=Health.FAULT, activity=Activity.UNKNOWN,
                      reason="camera connect failed", fault=str(exc))
            return
        # ADR-0009: the SDK identified the camera running its application firmware (Gate 1: 1.17.3.0)
        self._obs(connection=Connection.CONNECTED, health=Health.NORMAL, activity=Activity.IDLE,
                  firmware_mode=FirmwareMode.APPLICATION, attributes={"model": self.identity.get("model"), "fw": self.identity.get("fw_version"),
                              "exposure_ms": self.exposure_ms, "gain_db": self.gain_db},
                  reason="Chameleon connected (16-bit full sensor, manual exposure and gain)")

    def _apply_operating_settings(self) -> None:
        s = self._sdk
        s.set_format7_mono16_full()
        s.set_prop("auto_exposure", on=False, auto=False)
        s.set_prop("brightness", on=True, auto=False, abs_value=fc.BRIGHTNESS_PCT)
        self._set_gain(self.gain_db)
        self._set_shutter(self.exposure_ms)
        info = self._prop_info("shutter")
        self.exposure_range_ms = (max(0.01, info["abs_min"]), info["abs_max"])
        info = self._prop_info("gain")
        self.gain_range_db = (0.0, info["abs_max"])  # the UI's gain factor is >= 1x, i.e. >= 0 dB

    def _prop_info(self, name: str) -> dict:
        pi = fc.PropertyInfo()
        pi.type = fc.PROPERTIES[name]
        self._sdk._call("fc2GetPropertyInfo", self._sdk._ctx, C.byref(pi))
        return {"abs_min": float(pi.absMin), "abs_max": float(pi.absMax)}

    def _set_shutter(self, ms: float) -> None:
        self._sdk.set_prop("shutter", on=True, auto=False, abs_value=ms)
        got = self._sdk.get_prop("shutter")
        if got["auto"] or abs(got["abs_value"] - ms) > fc.TOL["shutter"](ms):
            raise RuntimeError(f"shutter reads {got['abs_value']:.4g} ms (auto={got['auto']}), requested {ms} ms")
        self.exposure_ms = got["abs_value"]

    def _set_gain(self, db: float) -> None:
        self._sdk.set_prop("gain", on=True, auto=False, abs_value=db)
        got = self._sdk.get_prop("gain")
        if got["auto"] or abs(got["abs_value"] - db) > fc.TOL["gain"](db):
            raise RuntimeError(f"gain reads {got['abs_value']:.4g} dB (auto={got['auto']}), requested {db} dB")
        self.gain_db = got["abs_value"]

    def invalidate_frames(self, reason: str = "") -> None:
        """Drop the next retrieved frame: the light changed, and the camera's next frame was exposed before
        the change (mono lag check 2026-11-16, ADR-0037). Called by InstrumentController after a light
        command. Exposure changes need no drop: that check found no exposure lag on this camera."""
        with self._io:
            self._stale = max(self._stale, STALE_FRAMES_AFTER_LIGHT)

    def disconnect(self) -> None:
        self.stop_stream("disconnect")
        self._restore_and_close()
        self._obs(connection=Connection.DISCONNECTED, activity=Activity.UNKNOWN,
                  reason="camera restored to power-up state and closed" if not self.restore_diffs
                  else f"camera closed; RESTORE NOT VERIFIED {self.restore_diffs}: power-cycle the instrument")

    def simulate_unexpected_disconnect(self) -> None:
        """The Instrument page's simulator test button: a real camera is never disturbed by it."""
        self._obs(reason="'simulate disconnect' ignored: this is the physical camera")

    def halt(self) -> None:
        self.stop_stream("halt")

    def _restore_and_close(self) -> None:
        with self._io:
            if self._connected and self._snapshot is not None:
                try:
                    fc.restore_state(self._sdk, self._snapshot)
                    self.restore_diffs = fc.compare_state(self._snapshot, self._sdk.snapshot())
                except Exception as exc:  # noqa: BLE001
                    self.restore_diffs = [f"restore failed: {exc}"]
            self._connected = False
            self._safe_close()

    def _safe_close(self) -> None:
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
        # shutter and gain are plain property writes, allowed while capturing (FlyCapture2); the Capture
        # page sets the channel's exposure with live view running, then acquire() restarts capture
        ctx.mark_started()
        try:
            with self._io:
                if isinstance(command, SetCameraExposure):
                    self._set_shutter(command.exposure_ms)
                else:
                    self._set_gain(command.gain_db)
        except Exception as exc:  # noqa: BLE001
            ctx.fail(str(exc), "DEVICE_ERROR")
            return
        ctx.complete(None)

    def verify_result(self, command: Command) -> tuple[bool, str]:
        if isinstance(command, SetCameraExposure) and abs(self.exposure_ms - command.exposure_ms) > fc.TOL["shutter"](command.exposure_ms):
            return False, f"exposure reads {self.exposure_ms:.4g} ms, commanded {command.exposure_ms} ms"
        if isinstance(command, SetCameraGain) and abs(self.gain_db - command.gain_db) > fc.TOL["gain"](command.gain_db):
            return False, f"gain reads {self.gain_db:.4g} dB, commanded {command.gain_db:.4g} dB"
        return True, ""

    def cancel(self, command_id: str) -> None:
        return None

    def read_position(self) -> Position | None:
        return None

    def recover(self) -> None:
        self.stop_stream("recover")
        self._obs(health=Health.NORMAL if self._connected else Health.FAULT, activity=Activity.IDLE, reason="recovered")

    def capabilities(self) -> DeviceCapabilities:
        exp = self.exposure_range_ms or (0.01, 60.0)  # Gate 1/2 ranges until connect reads them
        gain = self.gain_range_db or (0.0, 24.0)
        return DeviceCapabilities(
            device_id=self.device_id, backend_kind=BackendKind.PHYSICAL, source=CapabilitySource.VERIFIED,
            supported_commands=frozenset({"camera.set_exposure", "camera.set_gain"}),
            ranges=(ParameterRange(name="exposure_ms", limits=AxisLimits(minimum=exp[0], maximum=exp[1], unit="ms")),
                    ParameterRange(name="gain_db", limits=AxisLimits(minimum=gain[0], maximum=gain[1], unit="dB"))),
            evidence_ids=EVIDENCE,
            note="Chameleon CMLN-13S2M via FlyCapture2: 1296x964 12-bit, manual exposure/gain, RAM-only settings "
                 "(ADR-0036/0037)")

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
            with self._io:
                self._sdk._call("fc2StartCapture", self._sdk._ctx)
                self._sdk._capturing = True
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
        try:
            self._sdk._stop_capture()  # aborts a blocked RetrieveBuffer
        except Exception:  # noqa: BLE001, S110
            pass
        if t is not None and t is not threading.current_thread():
            t.join(RETRIEVE_TIMEOUT_S)
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
        img = fc.Image()
        self._sdk._call("fc2CreateImage", C.byref(img))
        consecutive_bad = 0
        try:
            while not self._stop.is_set():
                try:
                    self._sdk._call("fc2RetrieveBuffer", self._sdk._ctx, C.byref(img))
                except fc.Fc2Error as exc:
                    if self._stop.is_set():
                        return
                    if exc.code == fc.FC2_IMAGE_CONSISTENCY_ERROR and consecutive_bad < MAX_CONSECUTIVE_INCOMPLETE:
                        consecutive_bad += 1
                        self.incomplete_frames += 1
                        continue
                    if self._restart_capture(str(exc)):
                        consecutive_bad = 0
                        continue
                    if self._stop.is_set():
                        return
                    self._fail_stream(f"frame retrieval failed: {exc} (after {len(self._restarts)} restart(s) in "
                                      f"{RESTART_WINDOW_S:.0f} s)")
                    return
                consecutive_bad = 0
                with self._io:
                    if self._stale > 0:  # exposed (at least partly) before the last light change
                        self._stale -= 1
                        self.stale_dropped += 1
                        continue
                px, _name, _depth = fc.image_to_array(img)
                self._deliver(px >> BIT_SHIFT)
        finally:
            try:
                self._sdk._call("fc2DestroyImage", C.byref(img))
            except fc.Fc2Error:
                pass

    def _deliver(self, px: np.ndarray) -> None:
        with self._lock:
            s = self._stream
            if s is None:
                return
            idx = s["i"]
            s["i"] += 1
        extra = (("gain_db", f"{self.gain_db:.2f}"), ("camera_model", self.identity.get("model", "")),
                 ("bit_shift", str(BIT_SHIFT)), ("incomplete_frames_skipped", str(self.incomplete_frames)))
        if self._extra_fn is not None:
            extra += self._extra_fn()
        md = FrameMetadata(device_id=self.device_id, frame_index=idx, synthetic=False, acquisition_id=s["acq"],
                           channel=self._channel_fn() if self._channel_fn else None, exposure_ms=self.exposure_ms,
                           extra=extra)
        accepted = self.transport.push(px, md, owned=True)
        self.frames_produced += 1
        if self._degraded:  # first good frame after a restart
            self._degraded = False
            self._obs(health=Health.NORMAL, reason="camera stream recovered")
        if not accepted:
            threading.Thread(target=self.stop_stream, args=(f"frame {idx} refused by storage path",), daemon=True).start()
            return
        if s["n"] is not None and s["i"] >= s["n"]:
            threading.Thread(target=self.stop_stream, args=("requested frame count reached",), daemon=True).start()
            self._stop.set()

    def _restart_capture(self, why: str) -> bool:
        """Restart capture in place after a retrieval error (see MAX_STREAM_RESTARTS). True = streaming again."""
        now = time.monotonic()
        self._restarts = [t for t in self._restarts if now - t < RESTART_WINDOW_S]
        if len(self._restarts) >= MAX_STREAM_RESTARTS or self._stop.is_set():
            return False
        self._restarts.append(now)
        self.stream_restarts += 1
        self._obs(health=Health.WARNING, reason=f"camera stream restarted after: {why}")
        try:
            with self._io:
                self._sdk._stop_capture()
            if self._stop.wait(RESTART_PAUSE_S):
                return False  # stop requested meanwhile
            with self._io:
                self._sdk._call("fc2StartCapture", self._sdk._ctx)
                self._sdk._capturing = True
        except Exception:  # noqa: BLE001 - the caller declares the stream failed
            return False
        self._degraded = True
        return True

    def _fail_stream(self, message: str) -> None:
        self._obs(health=Health.FAULT, reason="camera stream failed", fault=message)
        threading.Thread(target=self.stop_stream, args=(message,), daemon=True).start()
