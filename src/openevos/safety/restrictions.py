"""Capability restrictions the operator confirmed (ADR-0070, Instrument Health Phase 2).

Health proposes; the operator confirms; this registry, owned by the SafetyManager, enforces. It is the only place a
Health restriction takes effect, through two doors:

* **Commands.** `HealthRestrictionInterlock` refuses the commands a restriction names (device id + command kind), in
  the SafetyManager's normal interlock pass. The SafetyManager never runs interlocks for a safe-direction command, so
  a restriction can never stop a light going off or live view stopping.
* **Runs and captures.** Every acquisition run (scan, time-lapse, Multi-D, Z-stack) enters `run_guard` with the
  capabilities it needs; a restricted capability refuses the run before it moves anything. `capture_refusal` refuses
  a single capture on a restricted camera.

A restriction never cuts a run short by itself. Confirmed while a run that needs the capability is going, it waits
("pending") and takes effect when the run ends; the operator may instead stop the run at its next safe point (the
run's own Stop: the current move or snap finishes, everything taken is written, the partial dataset is kept).

Release: Health lifts a restriction only with a verification that passed (`release(..., verification=...)`), or the
operator lifts it explicitly; both are recorded. A detector returning to normal is not a release (the kit's lifecycle).
Restrictions are kept in a file next to Health's journal, so a restart does not release them.

The registry never commands a device. It holds no reference to one.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
from collections.abc import Callable, Iterator, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from openevos.core.models import Command, DeviceSnapshot

RESTRICTIONS_FILE = "health-restrictions.json"
#: run capability names: the Health capability keys, plus one per camera ("imaging:<device id>")
MULTI_POSITION, DATA_SAVING, FOCUS_Z = "multi_position", "data_saving", "focus_z"


def imaging(device_id: str) -> str:
    return f"imaging:{device_id}"


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class Restriction:
    """One confirmed restriction: the kit's lock record (SAFETY_AND_RECOVERY.md, "Lock record")."""

    restriction_id: str
    policy_id: str
    policy_version: str
    title: str  # what is restricted, in words
    reason: str  # why, in words (the assessment's finding)
    #: run capabilities refused (see run_guard), e.g. ("multi_position",)
    capabilities: tuple[str, ...]
    #: commands refused: (device id, command kind)
    commands: tuple[tuple[str, str], ...]
    #: the assessment that led to it: detector, version, key, assessment id, evidence event ids
    trigger: dict[str, Any]
    release_conditions: tuple[str, ...]
    requested_at: str
    confirmed_by: str = "operator"
    state: str = "pending"  # pending -> active -> released
    activated_at: str = ""
    released_at: str = ""
    released_by: str = ""  # "verification" | "operator"
    verification: dict[str, Any] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)

    @property
    def active(self) -> bool:
        return self.state == "active"

    @property
    def open(self) -> bool:
        return self.state in ("pending", "active")

    def note(self, what: str, **data: Any) -> None:
        self.history.append({"at": _now().isoformat(), "what": what, **data})

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["commands"] = [list(c) for c in self.commands]
        return d

    @classmethod
    def from_json(cls, d: Mapping[str, Any]) -> Restriction:
        d = dict(d)
        d["capabilities"] = tuple(d.get("capabilities", ()))
        d["commands"] = tuple(tuple(c) for c in d.get("commands", ()))
        d["release_conditions"] = tuple(d.get("release_conditions", ()))
        return cls(**d)


@dataclass
class ActiveRun:
    run_id: str
    kind: str  # "Scan", "Multi-D", ...
    capabilities: frozenset[str]
    stop: Callable[[], None] | None
    started_at: str


class RestrictionRefused(RuntimeError):
    """A run or capture refused because a confirmed restriction covers what it needs."""


class RestrictionRegistry:
    """Thread-safe. Health (sampler and page threads), runners (worker threads) and the router call it."""

    def __init__(self, path: Path | str | None = None) -> None:
        self._lock = threading.RLock()
        self._items: dict[str, Restriction] = {}
        self._runs: dict[str, ActiveRun] = {}
        self.path: Path | None = None
        self.listeners: list[Callable[[str, Restriction], None]] = []
        #: the time activations and releases are stamped with (Health shares its clock, so verification windows and
        #: activation times agree; wall-clock UTC otherwise)
        self.now: Callable[[], datetime] = _now
        if path is not None:
            self.attach(path)

    # ------------------------------------------------------------------ persistence
    def attach(self, path: Path | str) -> None:
        """Keep restrictions in `path`; open ones already there come back (a restart does not release them)."""
        p = Path(path)
        with self._lock:
            self.path = p
            try:
                raw = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
            except (OSError, ValueError):
                raw = {}
            for d in raw.get("restrictions", []):
                try:
                    r = Restriction.from_json(d)
                except (TypeError, ValueError):
                    continue
                self._items.setdefault(r.restriction_id, r)
            for r in self._items.values():  # no run survives a restart: a pending one is now in force
                if r.state == "pending":
                    self._activate(r, "the app restarted with it pending")

    def _save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"schema_version": "1.0",
                                       "restrictions": [r.to_json() for r in self._items.values()]}, indent=1),
                           encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass  # the restriction holds in memory; Health shows the journal error separately

    # ------------------------------------------------------------------ reads
    def all(self) -> list[Restriction]:
        with self._lock:
            return list(self._items.values())

    def open(self) -> list[Restriction]:
        with self._lock:
            return [r for r in self._items.values() if r.open]

    def get(self, restriction_id: str) -> Restriction | None:
        with self._lock:
            return self._items.get(restriction_id)

    def runs(self) -> list[ActiveRun]:
        with self._lock:
            return list(self._runs.values())

    def conflicting_runs(self, capabilities: tuple[str, ...], commands: tuple[tuple[str, str], ...] = ()
                         ) -> list[ActiveRun]:
        caps = set(capabilities) | {_command_capability(c) for c in commands}
        with self._lock:
            return [r for r in self._runs.values() if r.capabilities & caps]

    # ------------------------------------------------------------------ the SafetyManager's doors
    def check_command(self, command: Command) -> list[str]:
        key = (command.device_id, command.kind)
        with self._lock:
            return [f"restricted by Health policy {r.policy_id} v{r.policy_version} (confirmed by the operator): "
                    f"{r.title}; release: {'; '.join(r.release_conditions)}"
                    for r in self._items.values() if r.active and key in r.commands]

    def run_refusal(self, capabilities: frozenset[str] | set[str]) -> list[str]:
        with self._lock:
            return [f"{r.title} (Health policy {r.policy_id} v{r.policy_version}, confirmed by the operator); "
                    f"release: {'; '.join(r.release_conditions)}"
                    for r in self._items.values() if r.active and set(r.capabilities) & set(capabilities)]

    def capture_refusal(self, camera_id: str) -> list[str]:
        return self.run_refusal({imaging(camera_id)})

    @contextlib.contextmanager
    def run(self, kind: str, capabilities: set[str] | frozenset[str], stop: Callable[[], None] | None = None
            ) -> Iterator[ActiveRun]:
        """Enter a run: refused (RestrictionRefused) when an active restriction covers what it needs; otherwise
        registered until it ends, so a restriction confirmed meanwhile waits for it."""
        caps = frozenset(capabilities)
        with self._lock:
            why = self.run_refusal(caps)
            if why:
                raise RestrictionRefused(f"{kind} refused: " + "; ".join(why))
            run = ActiveRun(uuid4().hex[:12], kind, caps, stop, _now().isoformat())
            self._runs[run.run_id] = run
        try:
            yield run
        finally:
            with self._lock:
                self._runs.pop(run.run_id, None)
                for r in list(self._items.values()):
                    if r.state == "pending" and not self.conflicting_runs(r.capabilities, r.commands):
                        self._activate(r, f"the {kind} run ended")

    # ------------------------------------------------------------------ requests (Health, after the operator confirms)
    def request(self, r: Restriction, *, stop_runs: bool = False) -> Restriction:
        """Put a confirmed restriction in force, or pending while a run needs what it restricts. With `stop_runs`,
        those runs are asked to stop at their next safe point (their own Stop)."""
        with self._lock:
            if r.restriction_id in self._items:
                return self._items[r.restriction_id]
            for other in self._items.values():  # one open restriction per policy and trigger key
                if other.open and other.policy_id == r.policy_id and other.trigger.get("key") == r.trigger.get("key"):
                    return other
            self._items[r.restriction_id] = r
            r.note("confirmed", by=r.confirmed_by)
            runs = self.conflicting_runs(r.capabilities, r.commands)
            if not runs:
                self._activate(r, "no run needed it")
            else:
                r.state = "pending"
                r.note("pending", runs=[f"{x.kind} ({x.run_id})" for x in runs])
                self._notify("pending", r)
                self._save()
        if runs and stop_runs:
            self.stop_runs(r.restriction_id)
        return r

    def stop_runs(self, restriction_id: str) -> list[str]:
        """Ask the runs a pending restriction waits for to stop at their next safe point."""
        with self._lock:
            r = self._items.get(restriction_id)
            runs = self.conflicting_runs(r.capabilities, r.commands) if r is not None and r.state == "pending" else []
            if r is not None and runs:
                r.note("runs asked to stop at their next safe point", runs=[x.kind for x in runs])
                self._save()
        stopped = []
        for x in runs:
            if x.stop is not None:
                try:
                    x.stop()
                    stopped.append(x.kind)
                except Exception:  # noqa: BLE001, S112 - one run's Stop failing must not stop the others
                    continue
        return stopped

    def release(self, restriction_id: str, *, by: str, verification: dict[str, Any] | None = None,
                note: str = "") -> Restriction:
        """Lift a restriction. `by="verification"` needs a verification that passed; `by="operator"` is the
        operator's explicit choice and is recorded as such."""
        with self._lock:
            r = self._items.get(restriction_id)
            if r is None or not r.open:
                raise KeyError(restriction_id)
            if by == "verification":
                if not verification or verification.get("passed") is not True:
                    raise PermissionError("a verification that passed is required to release it")
            elif by != "operator":
                raise PermissionError(f"unknown release origin {by!r}")
            r.state, r.released_at, r.released_by = "released", self.now().isoformat(), by
            r.verification = dict(verification or {})
            r.note("released", by=by, note=note, verification=r.verification)
            self._notify("released", r)
            self._save()
            return r

    def record(self, restriction_id: str, what: str, **data: Any) -> None:
        """Add to a restriction's audit history (e.g. a verification that did not pass yet)."""
        with self._lock:
            r = self._items.get(restriction_id)
            if r is not None:
                r.note(what, **data)
                self._save()

    def _activate(self, r: Restriction, why: str) -> None:
        r.state, r.activated_at = "active", self.now().isoformat()
        r.note("active", why=why)
        self._notify("active", r)
        self._save()

    def _notify(self, what: str, r: Restriction) -> None:
        for f in list(self.listeners):
            try:
                f(what, r)
            except Exception:  # noqa: BLE001, S112 - a listener (Health's journal) never breaks enforcement
                continue


def _command_capability(c: tuple[str, str]) -> str:
    """The run capability a command restriction touches: a stage or focus move restriction also waits for runs."""
    device, kind = c
    if kind == "stage.move_absolute":
        return MULTI_POSITION
    if kind == "focus.move_absolute":
        return FOCUS_Z
    if kind == "camera.snap":
        return imaging(device)
    return f"command:{device}:{kind}"


class HealthRestrictionInterlock:
    """The SafetyManager's interlock for confirmed Health restrictions (reads the registry only)."""

    name = "health"

    def __init__(self, registry: RestrictionRegistry) -> None:
        self.registry = registry

    def check(self, command: Command, world: Mapping[str, DeviceSnapshot]) -> list[str]:
        return self.registry.check_command(command)


def registry_of(obj: Any) -> RestrictionRegistry | None:
    """The registry behind a session, a controller, a capture service or core services, or None (a test double)."""
    for path in (("core", "safety"), ("ctl", "core", "safety"), ("safety",), ("session", "core", "safety")):
        cur = obj
        for a in path:
            cur = getattr(cur, a, None)
            if cur is None:
                break
        reg = getattr(cur, "restrictions", None) if cur is not None else None
        if isinstance(reg, RestrictionRegistry):
            return reg
    return None


@contextlib.contextmanager
def run_guard(owner: Any, kind: str, capabilities: set[str], stop: Callable[[], None] | None = None
              ) -> Iterator[None]:
    """`with run_guard(capture_or_session, "Scan", {...}, control.stop):` around a run's body."""
    reg = registry_of(owner)
    if reg is None:
        yield
        return
    with reg.run(kind, capabilities, stop):
        yield
