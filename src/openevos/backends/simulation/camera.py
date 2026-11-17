"""Simulated cameras: a 1296×964 12-bit mono camera and a colour camera of configurable size."""

from __future__ import annotations

import threading
import time
from typing import Any

import numpy as np

from openevos.backends.simulation.base import SimulatedBase
from openevos.backends.simulation.clock import SimClock
from openevos.backends.simulation.optics import SimulatedOptics
from openevos.backends.simulation.profile import CameraModel
from openevos.hal.interfaces import CameraDevice, DeviceError
from openevos.hal.types import CameraCapabilities, Frame


class SimulatedCamera(SimulatedBase, CameraDevice):
    def __init__(self, model: CameraModel, optics: SimulatedOptics, clock: SimClock, seed: int):
        self._init_sim(device_type="camera", display_name=model.display_name, clock=clock,
                       model=f"OpenEVOS simulator — {model.sensor}")
        self.model = model
        self._optics = optics
        self._rng = np.random.default_rng([seed & 0xFFFFFFFF, 1 if model.color else 0])
        self._exposure_ms = model.default_exposure_ms
        self._gain_db = 0.0
        self._index = 0
        self._latest: Frame | None = None
        self._live_thread: threading.Thread | None = None
        self._live_stop = threading.Event()
        self._render_lock = threading.Lock()
        self.live_fps = 0.0

    # -- capabilities & settings -----------------------------------------------------------
    def get_capabilities(self) -> CameraCapabilities:
        m = self.model
        return CameraCapabilities(
            width=m.width, height=m.height, bit_depth=m.bit_depth, color=m.color,
            pixel_size_um=m.pixel_size_um, exposure_range_ms=m.exposure_range_ms,
            gain_range_db=m.gain_range_db, max_fps=m.max_fps,
            pixel_format="RGB8" if m.color else f"MONO16 ({m.bit_depth}-bit data)", sensor=m.sensor,
        )

    def get_exposure_ms(self) -> float:
        return self._exposure_ms

    def set_exposure_ms(self, exposure_ms: float) -> None:
        lo, hi = self.model.exposure_range_ms
        if not lo <= exposure_ms <= hi:
            raise DeviceError(f"exposure {exposure_ms} ms outside [{lo}, {hi}]")
        with self._lock:
            self._require_ready()
            self._exposure_ms = float(exposure_ms)

    def get_gain_db(self) -> float:
        return self._gain_db

    def set_gain_db(self, gain_db: float) -> None:
        lo, hi = self.model.gain_range_db
        if not lo <= gain_db <= hi:
            raise DeviceError(f"gain {gain_db} dB outside [{lo}, {hi}]")
        with self._lock:
            self._require_ready()
            self._gain_db = float(gain_db)

    # -- acquisition -------------------------------------------------------------------------
    def _acquire(self) -> Frame:
        exposure, gain = self._exposure_ms, self._gain_db
        with self._render_lock:
            pixels, meta = self._optics.render(self.model, exposure, gain, self._rng)
            self._index += 1
            index = self._index
        meta.update(sensor=self.model.sensor, camera_pixel_size_um=self.model.pixel_size_um)
        frame = Frame(pixels=pixels, index=index, timestamp=time.time(), camera_role=self.model.role,
                      exposure_ms=exposure, gain_db=gain, bit_depth=self.model.bit_depth,
                      simulated=True, metadata=meta)
        self._latest = frame
        return frame

    def snap(self) -> Frame:
        with self._lock:
            self._require_ready()
            if self.is_live():
                raise DeviceError("camera is streaming; stop live first")
        self._clock.sleep(self._exposure_ms / 1000.0)
        return self._acquire()

    def start_live(self) -> None:
        with self._lock:
            self._require_ready()
            if self.is_live():
                return
            self._live_stop.clear()
            self._live_thread = threading.Thread(target=self._live_loop, name=f"live:{self.model.role}",
                                                 daemon=True)
            self._live_thread.start()

    def stop_live(self) -> None:
        self._live_stop.set()
        t = self._live_thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=2.0)
        self._live_thread = None
        self.live_fps = 0.0

    def is_live(self) -> bool:
        t = self._live_thread
        return t is not None and t.is_alive() and not self._live_stop.is_set()

    def get_latest_frame(self) -> Frame | None:
        return self._latest

    def halt(self) -> None:
        self.stop_live()

    def disconnect(self) -> None:
        self.stop_live()
        super().disconnect()

    def _live_loop(self) -> None:
        min_period = 1.0 / self.model.max_fps
        last = time.monotonic()
        while not self._live_stop.is_set():
            t0 = time.monotonic()
            try:
                self._acquire()
            except Exception as exc:  # noqa: BLE001 - a render failure faults the simulated camera
                self.inject_fault(f"live acquisition failed: {exc}")
                return
            period = max(min_period, self._clock.duration(self._exposure_ms / 1000.0))
            remaining = period - (time.monotonic() - t0)
            if remaining > 0 and self._live_stop.wait(remaining):
                break
            now = time.monotonic()
            dt = now - last
            last = now
            if dt > 0:
                self.live_fps = 0.8 * self.live_fps + 0.2 * (1.0 / dt) if self.live_fps else 1.0 / dt

    def _busy_message(self) -> str:
        return "acquiring"

    def _details(self) -> dict[str, Any]:
        return {"exposure_ms": self._exposure_ms, "gain_db": self._gain_db, "live": self.is_live(),
                "resolution": f"{self.model.width}×{self.model.height}",
                "live_fps": round(self.live_fps, 1)}
