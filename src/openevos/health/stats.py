"""Robust statistics and versioned baselines for Instrument Health, Phase 1 (ADR-0069).

A baseline is the median, the MAD and the 5th/95th percentiles of one metric in one operating regime (for example the
X following error while the stage is idle). It is versioned per (metric, regime), tied to the instrument's identity
(the fingerprint: device models, firmware, the motor port) and labelled with how it was made:

* ``qualification`` is "not-qualified" until an operator qualifies it on the instrument. A baseline made on the
  simulator can never be qualified. Qualification is a physical gate the kit keeps for the AMAFD1000 itself, so
  Phase 1 adds no way to qualify one from the page.
* A baseline whose fingerprint differs from the session's is invalid: a firmware or hardware change ends it, and the
  rules that use it stop (they say "no valid baseline", never a guess).

Pure functions and a small JSON store; nothing here touches a device.
"""

from __future__ import annotations

import json
import math
import os
import threading
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from openevos.core.models import utcnow

#: scales the MAD to a standard deviation for normal data
MAD_SCALE = 1.4826
#: fewest samples a baseline may be built from
MIN_BASELINE_SAMPLES = 30
BASELINE_FILE = "health-baselines.json"
STORE_SCHEMA = "1.0"


# ====================================================================================================== statistics
def median(xs: Sequence[float]) -> float:
    if not xs:
        raise ValueError("median of no values")
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    return float(s[mid]) if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def mad(xs: Sequence[float], center: float | None = None) -> float:
    """Median absolute deviation (unscaled)."""
    c = median(xs) if center is None else center
    return median([abs(x - c) for x in xs])


def quantile(xs: Sequence[float], q: float) -> float:
    """Linear-interpolated quantile, q in [0, 1]."""
    if not xs:
        raise ValueError("quantile of no values")
    if not 0.0 <= q <= 1.0:
        raise ValueError("q must be in [0, 1]")
    s = sorted(xs)
    pos = q * (len(s) - 1)
    lo = math.floor(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def robust_z(x: float, center: float, mad_value: float, floor: float) -> float:
    """How many robust standard deviations x lies above the center. `floor` stops a zero MAD (a perfectly steady
    simulator, or a quantised encoder that reads the same count every time) from turning one count into infinity."""
    spread = max(MAD_SCALE * mad_value, floor)
    return (x - center) / spread


def linear_slope(points: Sequence[tuple[float, float]]) -> float | None:
    """Least-squares slope dy/dt of (t, y) points; None with fewer than 2 distinct times."""
    if len(points) < 2:
        return None
    n = len(points)
    mt = sum(t for t, _ in points) / n
    my = sum(y for _, y in points) / n
    den = sum((t - mt) ** 2 for t, _ in points)
    if den <= 0:
        return None
    return sum((t - mt) * (y - my) for t, y in points) / den


# ====================================================================================================== baselines
@dataclass(frozen=True)
class Baseline:
    metric: str  # e.g. "xy_stage.following_error.X"
    regime: str  # e.g. "idle"
    unit: str
    n: int
    median: float
    mad: float
    p05: float
    p95: float
    created_utc: str
    #: what the samples came from: "simulator" or "instrument"
    source: str
    #: device models, firmware and the motor port it was made with; a different fingerprint ends the baseline
    fingerprint: dict[str, str] = field(default_factory=dict)
    version: int = 1
    qualification: str = "not-qualified"
    qualified_by: str = ""
    notes: str = ""

    @property
    def baseline_id(self) -> str:
        return f"{self.metric}@{self.regime}"

    @property
    def qualified(self) -> bool:
        return self.qualification == "qualified"

    def ref(self) -> dict[str, Any]:
        """The kit's fault_assessment.baseline block."""
        return {"id": self.baseline_id, "version": str(self.version),
                "regime": self.regime if self.qualified else f"{self.regime}, not-qualified"}


def build_baseline(metric: str, regime: str, unit: str, values: Iterable[float], *, source: str,
                   fingerprint: dict[str, str], now: datetime | None = None, notes: str = "") -> Baseline:
    xs = [float(v) for v in values if v is not None and math.isfinite(float(v))]
    if len(xs) < MIN_BASELINE_SAMPLES:
        raise ValueError(f"{metric} ({regime}): {len(xs)} samples, a baseline needs at least {MIN_BASELINE_SAMPLES}")
    m = median(xs)
    return Baseline(metric, regime, unit, len(xs), m, mad(xs, m), quantile(xs, 0.05), quantile(xs, 0.95),
                    (now or utcnow()).isoformat(), source, dict(fingerprint), notes=notes)


def fingerprint_matches(b: Baseline, fp: dict[str, str]) -> bool:
    """Every key the baseline recorded must still read the same. Keys the session lacks count as changed."""
    return all(fp.get(k) == v for k, v in b.fingerprint.items())


class BaselineStore:
    """Versioned baselines in one JSON file (a temporary file, then an atomic replace). Thread-safe.
    `path=None` keeps them in memory only (tests, or no writable folder)."""

    def __init__(self, path: Path | str | None):
        self.path = Path(path) if path is not None else None
        self._lock = threading.Lock()
        self._items: list[Baseline] = []
        self.load_error = ""
        if self.path is not None and self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                self._items = [Baseline(**{k: v for k, v in d.items() if k in Baseline.__dataclass_fields__})
                               for d in raw.get("baselines", [])]
            except Exception as exc:  # noqa: BLE001 - a bad file means no baselines, said plainly
                self.load_error = f"{type(exc).__name__}: {exc}"
                self._items = []

    def all(self) -> list[Baseline]:
        with self._lock:
            return list(self._items)

    def latest(self, metric: str, regime: str) -> Baseline | None:
        with self._lock:
            found = [b for b in self._items if b.metric == metric and b.regime == regime]
        return max(found, key=lambda b: b.version) if found else None

    def valid(self, metric: str, regime: str, fingerprint: dict[str, str]) -> tuple[Baseline | None, str]:
        """The latest baseline if it still fits this instrument, else (None, why)."""
        b = self.latest(metric, regime)
        if b is None:
            return None, "no baseline"
        if not fingerprint_matches(b, fingerprint):
            changed = sorted(k for k, v in b.fingerprint.items() if fingerprint.get(k) != v)
            return None, f"baseline v{b.version} invalid: {', '.join(changed)} changed since it was made"
        return b, ""

    def add(self, b: Baseline) -> Baseline:
        """Store b as the next version of its (metric, regime). Earlier versions stay (history)."""
        with self._lock:
            prev = [x.version for x in self._items if x.metric == b.metric and x.regime == b.regime]
            b = replace(b, version=max(prev, default=0) + 1)
            self._items.append(b)
            self._save()
        return b

    def qualify(self, metric: str, regime: str, by: str) -> Baseline:
        """Mark the latest baseline qualified. Refused for a simulator baseline. Phase 1 has no caller in the app:
        qualification happens on the instrument, with the operator (the kit's physical gate)."""
        if not by.strip():
            raise ValueError("qualification needs the operator's name")
        with self._lock:
            found = [x for x in self._items if x.metric == metric and x.regime == regime]
            if not found:
                raise KeyError(f"no baseline for {metric} ({regime})")
            b = max(found, key=lambda x: x.version)
            if b.source != "instrument":
                raise ValueError("a simulator baseline cannot be qualified")
            q = replace(b, qualification="qualified", qualified_by=by.strip())
            self._items[self._items.index(b)] = q
            self._save()
        return q

    def to_json(self) -> dict[str, Any]:
        with self._lock:
            return {"schema_version": STORE_SCHEMA, "baselines": [asdict(b) for b in self._items]}

    def _save(self) -> None:
        if self.path is None:
            return
        data = {"schema_version": STORE_SCHEMA, "baselines": [asdict(b) for b in self._items]}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)
