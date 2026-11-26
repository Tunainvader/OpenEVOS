"""Simulator fault injection for Instrument Health, Phase 1 (ADR-0069).

The kit asks for repeatable detection, tested by injecting faults into the simulator, and forbids injecting faults on
the instrument by sending guessed commands. So every injector here only turns a knob the simulator already has (the
emulated drive chain's registers and test hooks, the simulated camera's incomplete frames, a simulated device's fault)
or replaces Health's own disk reading. Building one refuses any session with a physical device or a real motor port:
`NotSimulated` is raised before anything is touched.

Each injection is journaled through the Health service (when given) as `simulation.injected`, provenance Demo, so an
exported diagnostic bundle shows what was injected next to what the rules saw.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from openevos.core.models import BackendKind
from openevos.health.assess import DiskView
from openevos.health.model import Provenance


class NotSimulated(RuntimeError):
    """The session has a physical device: fault injection is refused."""


def physical_parts(session: Any) -> list[str]:
    """Every device or motor port in the session that is not simulated (empty = all simulated)."""
    out = [d for d, s in session.core.store.snapshot_all().items() if s.backend_kind is not BackendKind.SIMULATED]
    for svc in getattr(session, "telemetry", []) or []:
        if not getattr(svc, "emulated", False) or getattr(svc, "simulated_chain", None) is None:
            out.append(f"motor port {getattr(svc, 'port_label', '?')}")
    return sorted(out)


class SimulatorFaultInjector:
    def __init__(self, session: Any, health: Any = None):
        parts = physical_parts(session)
        if parts:
            raise NotSimulated("fault injection is for the simulator only; this session has: " + ", ".join(parts))
        self.session = session
        self.health = health

    # ------------------------------------------------------------------ helpers
    def _note(self, subsystem: str, text: str, **data: Any) -> None:
        if self.health is not None:
            self.health._emit("simulation.injected", subsystem, "info", f"simulator injection: {text}",
                              provenance=Provenance.DEMO, source="SimulatorFaultInjector", data=data)

    @property
    def chain(self) -> Any:
        for svc in getattr(self.session, "telemetry", []) or []:
            chain = getattr(svc, "simulated_chain", None)
            if chain is not None:
                return chain
        raise NotSimulated("no emulated drive chain in this session (build it with telemetry port 'sim')")

    def _backend(self, device_id: str) -> Any:
        rec = self.session.core.devices.get(device_id)
        if rec is None:
            raise KeyError(f"no device {device_id}")
        return rec.backend

    # ------------------------------------------------------------------ X/Y stage (emulated drive chain)
    def offset_at_rest(self, axis: str, counts: int) -> None:
        """Make an axis at rest read `counts` from its command (CPOS moved, APOS untouched: no encoder drift, so the
        drift monitor does not lock; above 3 counts it warns)."""
        c = self.chain
        with c._lock:
            c.values[(axis, "CPOS")] = c.values[(axis, "APOS")] - int(counts)
        self._note("xy_stage", f"{axis} at rest {counts:+d} counts from its command", axis=axis, counts=counts)

    def land_off(self, axis: str, counts: int, moves: int = 1) -> None:
        """The next `moves` moves of an axis end `counts` off their target (the ADR-0061 landing test hook)."""
        self.chain.land_off[axis] = [int(counts)] * moves
        self._note("xy_stage", f"next {moves} {axis} move(s) land {counts:+d} counts off", axis=axis, counts=counts,
                   moves=moves)

    def trip_on_move(self, axis: str, spec: dict[str, Any]) -> None:
        """The next move of an axis trips its drive (the ADR-0067 test hook)."""
        self.chain.trip_on_move[axis] = dict(spec)
        self._note("xy_stage", f"next {axis} move trips its drive", axis=axis, spec=dict(spec))

    def stall_moves(self, stalled: bool = True) -> None:
        """Moves never finish (the drive-chain time-out hook)."""
        self.chain.moves_complete = not stalled
        self._note("xy_stage", "moves never finish" if stalled else "moves finish again", stalled=stalled)

    # ------------------------------------------------------------------ simulated devices
    def camera_incomplete(self, device_id: str, every: int) -> None:
        """Every `every`-th frame of the camera's stream is incomplete and skipped (0 stops it)."""
        b = self._backend(device_id)
        if not hasattr(b, "inject_incomplete_every"):
            raise TypeError(f"{device_id} has no incomplete-frame simulation")
        b.inject_incomplete_every = int(every)
        self._note("mono_camera", f"{device_id}: every {every}th frame incomplete" if every else
                   f"{device_id}: no more incomplete frames", device_id=device_id, every=every)

    def camera_disconnect(self, device_id: str) -> None:
        self._backend(device_id).simulate_unexpected_disconnect()
        self._note("mono_camera", f"{device_id}: unexpected disconnect", device_id=device_id)

    def fail_next(self, device_id: str, message: str = "simulated failure") -> None:
        """The device's next command fails (the simulator's own fail_next hooks)."""
        b = self._backend(device_id)
        for attr in ("fail_next", "fail_next_move"):
            if hasattr(b, attr):
                setattr(b, attr, message)
                self._note("instrument", f"{device_id}: next command fails ({message})", device_id=device_id)
                return
        raise TypeError(f"{device_id} has no fail-next hook")

    # ------------------------------------------------------------------ Health's own readings
    def disk_filling(self, start_free_bytes: int, bytes_per_s: float, *, t0: datetime | None = None,
                     total_bytes: int = 500 * 10**9) -> Callable[[Path, datetime], DiskView]:
        """Replace the Health service's disk reading with a drive that loses `bytes_per_s` from `start_free_bytes`.
        Health's reading only: no file is written."""
        if self.health is None:
            raise ValueError("disk_filling needs the Health service")
        origin: list[datetime] = [t0] if t0 is not None else []

        def reader(path: Path, now: datetime) -> DiskView:
            if not origin:
                origin.append(now)
            free = max(0, int(start_free_bytes - bytes_per_s * (now - origin[0]).total_seconds()))
            return DiskView(str(path), free, total_bytes, "", now)

        self.health.disk_reader = reader
        self.health._disk = None
        self._note("storage", f"data drive filling at {bytes_per_s / 1e6:.0f} MB/s from "
                   f"{start_free_bytes / 1e9:.0f} GB free (Health's reading only)",
                   start_free_bytes=start_free_bytes, bytes_per_s=bytes_per_s)
        return reader
