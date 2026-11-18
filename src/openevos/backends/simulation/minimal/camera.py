"""Simulated camera: reproducible synthetic frames (noisy background + drifting Gaussian 'cells').

Frames are produced on the injected clock at a configurable rate and pushed into the
FrameTransport with `owned=True` (each frame is a fresh array). Metadata marks them synthetic.
If the transport refuses a frame that belongs to an acquisition (storage fault/back-pressure),
the camera stops the stream immediately — it never keeps acquiring into a broken pipeline.
"""

from __future__ import annotations

import itertools
import threading
from collections.abc import Callable

import numpy as np

from openevos.core.clock import Clock, TimerHandle
from openevos.core.frame_transport import FrameMetadata, FrameTransport
from openevos.core.models import (
    Activity,
    BackendKind,
    Command,
    Connection,
    DeviceKind,
    Health,
    Position,
    SetCameraExposure,
)
from openevos.hal.backend import ExecutionContext, Reporter
from openevos.safety.capabilities import (
    AxisLimits,
    CapabilitySource,
    DeviceCapabilities,
    ParameterRange,
)


class SimCamera:
    kind = DeviceKind.CAMERA
    backend_kind = BackendKind.SIMULATED

    def __init__(self, device_id: str, clock: Clock, transport: FrameTransport, *, width: int = 640,
                 height: int = 480, fps: float = 15.0, seed: int = 0, n_objects: int = 12):
        if fps <= 0 or width < 16 or height < 16:
            raise ValueError("invalid simulator camera geometry or rate")
        self.device_id, self.clock, self.transport = device_id, clock, transport
        self.physical_id = f"sim:{device_id}"
        self.width, self.height, self.fps = width, height, fps
        self.exposure_ms = 50.0
        self.channel = "sim-brightfield"
        self._rng = np.random.default_rng(seed)
        self._objs = np.column_stack([
            self._rng.uniform(0, width, n_objects), self._rng.uniform(0, height, n_objects),
            self._rng.uniform(4, 12, n_objects), self._rng.uniform(800, 2500, n_objects),
            self._rng.normal(0, 0.8, n_objects), self._rng.normal(0, 0.8, n_objects),
        ])  # x, y, sigma, amplitude, vx, vy
        self._lock = threading.RLock()
        self._seq = itertools.count(1)
        self._report: Reporter | None = None
        self._timer: TimerHandle | None = None
        self._stream: dict | None = None
        self.frames_produced = 0

    # ------------------------------------------------------------------ DeviceBackend
    def attach(self, reporter: Reporter) -> None:
        self._report = reporter

    def _obs(self, **fields) -> None:
        if self._report is not None:
            self._report(seq=next(self._seq), **fields)

    def connect(self) -> None:
        self._obs(connection=Connection.CONNECTING, activity=Activity.INITIALIZING, reason="simulator connecting")
        self._obs(connection=Connection.CONNECTED, health=Health.NORMAL, activity=Activity.IDLE, reason="simulator connected")  # noqa: E501

    def disconnect(self) -> None:
        self.stop_stream("disconnect")
        self._obs(connection=Connection.DISCONNECTED, activity=Activity.UNKNOWN, reason="disconnected")

    def simulate_unexpected_disconnect(self) -> None:
        self.stop_stream("simulated disconnect")
        self._obs(connection=Connection.DISCONNECTED, health=Health.FAULT, activity=Activity.UNKNOWN,
                  reason="simulated cable pull", fault="simulated camera disconnect")

    def start(self, command: Command, ctx: ExecutionContext) -> None:
        if not isinstance(command, SetCameraExposure):
            ctx.fail(f"camera cannot execute {command.kind}", "UNSUPPORTED")
            return
        ctx.mark_started()
        with self._lock:
            self.exposure_ms = command.exposure_ms
        ctx.complete(None)

    def cancel(self, command_id: str) -> None:  # exposure changes complete immediately
        return None

    def read_position(self) -> Position | None:
        return None

    def recover(self) -> None:
        self.stop_stream("recover")
        self._obs(health=Health.NORMAL, activity=Activity.IDLE, reason="recovered")

    def capabilities(self) -> DeviceCapabilities:
        return DeviceCapabilities(device_id=self.device_id, backend_kind=BackendKind.SIMULATED,
                                  source=CapabilitySource.SIMULATED,
                                  supported_commands=frozenset({"camera.set_exposure"}),
                                  ranges=(ParameterRange(name="exposure_ms", limits=AxisLimits(
                                      minimum=0.01, maximum=60_000.0, unit="ms")),),
                                  note="Simulator camera; synthetic frames.")

    # ------------------------------------------------------------------ streaming
    def start_stream(self, *, acquisition_id: str | None = None, n_frames: int | None = None,
                     on_finished: Callable[[str], None] | None = None) -> None:
        with self._lock:
            if self._stream is not None:
                raise RuntimeError("stream already running")
            self._stream = {"acq": acquisition_id, "n": n_frames, "i": 0, "done": on_finished}
        self._obs(activity=Activity.ACQUIRING, reason=f"streaming (acquisition={acquisition_id})")
        self._schedule()

    def stop_stream(self, reason: str = "stopped") -> None:
        with self._lock:
            s, self._stream = self._stream, None
            if self._timer:
                self._timer.cancel()
                self._timer = None
        if s is not None:
            self._obs(activity=Activity.IDLE, reason=f"stream stopped: {reason}")
            if s["done"]:
                s["done"](reason)

    @property
    def streaming(self) -> bool:
        return self._stream is not None

    def _schedule(self) -> None:
        with self._lock:
            if self._stream is not None:
                self._timer = self.clock.call_later(1.0 / self.fps, self._tick)

    def _tick(self) -> None:
        with self._lock:
            s = self._stream
            if s is None:
                return
            idx = s["i"]
            s["i"] += 1
            img = self.render()
            md = FrameMetadata(device_id=self.device_id, frame_index=idx, synthetic=True,
                               acquisition_id=s["acq"], channel=self.channel, exposure_ms=self.exposure_ms)
        accepted = self.transport.push(img, md, owned=True)
        self.frames_produced += 1
        if not accepted:
            self.stop_stream(f"frame {idx} refused by storage path")
            return
        if s["n"] is not None and s["i"] >= s["n"]:
            self.stop_stream("requested frame count reached")
            return
        self._schedule()

    def render(self) -> np.ndarray:
        """One synthetic 16-bit frame; objects drift and wrap around the field."""
        h, w = self.height, self.width
        gain = self.exposure_ms / 50.0
        img = self._rng.normal(300.0 * gain, 20.0, (h, w)).astype(np.float32)
        o = self._objs
        o[:, 0] = (o[:, 0] + o[:, 4]) % w
        o[:, 1] = (o[:, 1] + o[:, 5]) % h
        for x, y, sig, amp, _vx, _vy in o:
            r = int(4 * sig) + 1
            x0, x1 = max(0, int(x) - r), min(w, int(x) + r + 1)
            y0, y1 = max(0, int(y) - r), min(h, int(y) + r + 1)
            if x0 >= x1 or y0 >= y1:
                continue
            yy, xx = np.ogrid[y0:y1, x0:x1]
            img[y0:y1, x0:x1] += amp * gain * np.exp(-((xx - x) ** 2 + (yy - y) ** 2) / (2 * sig * sig))
        return np.clip(img, 0, 65535).astype(np.uint16)
