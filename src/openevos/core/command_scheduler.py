"""Resource reservations and per-device serialized execution (handoff §16.4).

* `ResourceTable.try_reserve()` is atomic: two commands can never both reserve a conflicting
  resource, even if they validated against the same idle snapshot.
* Exclusive conflicts with any other holder; shared conflicts only with an exclusive holder.
* Reservations are released on terminal states, except TIMED_OUT, which keeps them until an
  explicit recovery (the physical state is uncertain).
* `DeviceWorker` runs backend `start()` calls for one device in order, off the UI thread.
  `ManualWorker` lets tests control exactly when queued work runs.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable

from openevos.safety.interlocks import ResourceClaim

log = logging.getLogger(__name__)


class ResourceTable:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._exclusive: dict[str, str] = {}  # resource -> owner
        self._shared: dict[str, set[str]] = {}  # resource -> owners
        self._claims: dict[str, ResourceClaim] = {}

    def try_reserve(self, owner: str, claim: ResourceClaim) -> list[str]:
        """Reserve all resources in `claim` for `owner`, or nothing. Returns conflicts (empty = ok)."""
        with self._lock:
            if owner in self._claims:
                return [f"owner {owner} already holds a reservation"]
            conflicts: list[str] = []
            for res in sorted(claim.exclusive):
                holder = self._exclusive.get(res)
                if holder and holder != owner:
                    conflicts.append(f"{res} is exclusively held by {holder}")
                for other in self._shared.get(res, set()) - {owner}:
                    conflicts.append(f"{res} must stay stable for {other}")
            for res in sorted(claim.shared):
                holder = self._exclusive.get(res)
                if holder and holder != owner:
                    conflicts.append(f"{res} is exclusively held by {holder}")
            if conflicts:
                return conflicts
            for res in claim.exclusive:
                self._exclusive[res] = owner
            for res in claim.shared:
                self._shared.setdefault(res, set()).add(owner)
            self._claims[owner] = claim
            return []

    def release(self, owner: str) -> bool:
        with self._lock:
            claim = self._claims.pop(owner, None)
            if claim is None:
                return False
            for res in claim.exclusive:
                if self._exclusive.get(res) == owner:
                    del self._exclusive[res]
            for res in claim.shared:
                owners = self._shared.get(res)
                if owners:
                    owners.discard(owner)
                    if not owners:
                        del self._shared[res]
            return True

    def holders(self) -> dict[str, ResourceClaim]:
        with self._lock:
            return dict(self._claims)

    def owners_of(self, res: str) -> set[str]:
        with self._lock:
            out = set(self._shared.get(res, set()))
            if res in self._exclusive:
                out.add(self._exclusive[res])
            return out


class DeviceWorker:
    """One background thread per device; runs submitted callables in FIFO order."""

    def __init__(self, name: str):
        self._q: queue.Queue[Callable[[], None] | None] = queue.Queue()
        self._thread = threading.Thread(target=self._run, name=f"openevos-worker-{name}", daemon=True)
        self._thread.start()

    def submit(self, fn: Callable[[], None]) -> None:
        self._q.put(fn)

    def stop(self, timeout: float = 2.0) -> None:
        self._q.put(None)
        self._thread.join(timeout)

    def _run(self) -> None:
        while True:
            fn = self._q.get()
            if fn is None:
                return
            try:
                fn()
            except Exception:
                log.exception("device worker task failed")


class ManualWorker:
    """Test double: queued work runs only when `run_pending()` is called."""

    def __init__(self, name: str = "manual"):
        self._pending: list[Callable[[], None]] = []
        self._lock = threading.Lock()

    def submit(self, fn: Callable[[], None]) -> None:
        with self._lock:
            self._pending.append(fn)

    def run_pending(self) -> int:
        with self._lock:
            work, self._pending = self._pending, []
        for fn in work:
            fn()
        return len(work)

    def stop(self, timeout: float = 0) -> None:
        with self._lock:
            self._pending.clear()
