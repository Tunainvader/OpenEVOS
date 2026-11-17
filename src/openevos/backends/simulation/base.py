"""Common behaviour of simulated devices: identity labelled *simulated*, lifecycle, faults."""

from __future__ import annotations

import threading
from typing import Any

from openevos.backends.simulation.clock import SimClock
from openevos.hal.interfaces import DeviceError
from openevos.hal.status_model import RuntimeState
from openevos.hal.types import DeviceIdentity, DeviceStatus

SIM_NOTE = ("Simulated device. Its shape follows what the vendor software expects; nothing about it "
            "is verified on the instrument.")


class SimulatedBase:
    """Mixin providing identify/get_status/connect/disconnect for simulated devices."""

    def _init_sim(self, *, device_type: str, display_name: str, clock: SimClock,
                  model: str = "OpenEVOS simulator") -> None:
        self._clock = clock
        self._lock = threading.RLock()
        self._identity = DeviceIdentity(
            device_type=device_type, display_name=f"{display_name} (simulated)",
            backend="simulation", simulated=True, manufacturer="OpenEVOS", model=model,
            serial_number=None, firmware_version=None, transport="in-process simulation",
            notes=SIM_NOTE,
        )
        self._connected = False
        self._fault: str | None = None

    # -- HardwareDevice --------------------------------------------------------------------
    def identify(self) -> DeviceIdentity:
        return self._identity

    def connect(self) -> None:
        with self._lock:
            self._connected = True

    def disconnect(self) -> None:
        with self._lock:
            self._connected = False

    def get_status(self) -> DeviceStatus:
        with self._lock:
            if not self._connected:
                state, msg = RuntimeState.DISCONNECTED, "not connected"
            elif self._fault:
                state, msg = RuntimeState.FAULTED, self._fault
            elif self.is_busy():  # type: ignore[attr-defined]
                state, msg = RuntimeState.BUSY, self._busy_message()
            else:
                state, msg = RuntimeState.READY, ""
            return DeviceStatus(state=state, message=msg, simulated=True, evidence_stage=None,
                                details=self._details())

    # -- simulation-only controls (not part of the HAL; used by tests) ----------------------
    def inject_fault(self, message: str) -> None:
        with self._lock:
            self._fault = message

    def clear_fault(self) -> None:
        with self._lock:
            self._fault = None

    # -- helpers --------------------------------------------------------------------------
    def _require_ready(self) -> None:
        if not self._connected:
            raise DeviceError(f"{self._identity.display_name} is not connected")
        if self._fault:
            raise DeviceError(f"{self._identity.display_name} is faulted: {self._fault}")

    def _busy_message(self) -> str:
        return "moving"

    def _details(self) -> dict[str, Any]:
        return {}
