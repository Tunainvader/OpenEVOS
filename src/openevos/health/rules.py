"""Deterministic, versioned rules for Instrument Health, Phase 1 (ADR-0069).

Every rule reads only what Health already records: the journal's recent events, the metric series the service samples
(backend counters, following error, free space) and the baselines. Each rule has a name and a version, a persistence
(`raise_after` consecutive evaluations with a hit before it raises) and a hold (`clear_after_s` without a hit before it
resolves), so one noisy reading neither raises nor clears it.

Rules only ever say WATCH. They restrict nothing and release nothing: the StageMonitor and the SafetyManager keep that
authority. Limits without a baseline are labelled "provisional, not qualified": they were chosen on the simulator and
the recorded instrument logs, not qualified on the AMAFD1000 (the kit's physical gate). Hypotheses are never confirmed.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import TYPE_CHECKING, Any

from openevos.core.models import new_id
from openevos.health.model import (
    Assessment,
    DetectorStatus,
    HealthEvent,
    HealthState,
    Provenance,
    capabilities_affected_by,
)
from openevos.health.stats import BaselineStore, linear_slope, median, robust_z

if TYPE_CHECKING:
    from openevos.health.assess import HealthView

SERIES_MAXLEN = 4000  # ~66 min at 1 Hz
MAX_EVIDENCE = 50
PROVISIONAL = "provisional, not qualified"


# ====================================================================================================== series
class MetricSeries:
    """In-memory time series per metric key: (t seconds, value, regime). The sampler thread writes; the page and the
    diagnostic export read from other threads, so every access copies under a lock."""

    def __init__(self, maxlen: int = SERIES_MAXLEN):
        self._d: dict[str, deque[tuple[float, float, str]]] = {}
        self._maxlen = maxlen
        self._lock = threading.Lock()

    def record(self, key: str, t: float, value: float, regime: str = "") -> None:
        with self._lock:
            d = self._d.setdefault(key, deque(maxlen=self._maxlen))
            if d and t <= d[-1][0]:
                return  # a repeat or out of order: dropped, never re-sorted
            d.append((t, float(value), regime))

    def _copy(self, key: str) -> list[tuple[float, float, str]]:
        with self._lock:
            return list(self._d.get(key, ()))

    def keys(self, prefix: str = "") -> list[str]:
        with self._lock:
            return sorted(k for k in self._d if k.startswith(prefix))

    def points(self, key: str, since: float | None = None, regime: str | None = None) -> list[tuple[float, float]]:
        return [(t, v) for t, v, r in self._copy(key) if (since is None or t >= since)
                and (regime is None or r == regime)]

    def last(self, key: str) -> tuple[float, float, str] | None:
        with self._lock:
            d = self._d.get(key)
            return d[-1] if d else None

    def delta(self, key: str, since: float) -> float | None:
        """How much a counter grew since `since`: last value minus the last value at or before `since` (or the first
        value inside the window). None without two readings. A counter that went down (a backend was re-created)
        counts from its new start."""
        d = self._copy(key)
        if not d:
            return None
        before = [v for t, v, _ in d if t <= since]
        inside = [v for t, v, _ in d if t > since]
        if not inside:
            return None
        start = before[-1] if before else inside[0]
        if not before and len(inside) < 2:
            return None
        growth = 0.0
        prev = start
        for v in inside:
            growth += v - prev if v >= prev else v
            prev = v
        return growth


# ====================================================================================================== engine
@dataclass(frozen=True)
class RuleInput:
    now: datetime
    view: HealthView
    events: tuple[HealthEvent, ...]  # recent events, oldest first
    series: MetricSeries
    baselines: BaselineStore
    fingerprint: dict[str, str] = field(default_factory=dict)

    @property
    def t(self) -> float:
        return self.now.timestamp()

    def subsystem_of(self, did: str) -> str:
        return next((k for k, ds in self.view.devices.items() if did in ds), "instrument")

    def simulated(self, did: str) -> bool:
        snap = self.view.snapshots.get(did)
        return snap is not None and getattr(snap.backend_kind, "value", "") == "simulated"


@dataclass(frozen=True)
class Hit:
    key: str
    subsystem: str
    title: str
    detail: str
    evidence: tuple[str, ...] = ()
    baseline: dict[str, Any] | None = None
    limit: str = ""
    simulated: bool = False
    data: dict[str, Any] = field(default_factory=dict)


class Detector:
    name = ""
    version = "1"
    subsystem = ""
    title = ""
    raise_after = 1
    clear_after_s = 60.0
    hypotheses: tuple[str, ...] = ()
    checks: tuple[str, ...] = ()

    def status(self, inp: RuleInput) -> tuple[bool, str]:
        return True, ""

    def check(self, inp: RuleInput) -> list[Hit]:
        raise NotImplementedError


@dataclass
class _Track:
    streak: int = 0
    last_hit: datetime | None = None
    active: Assessment | None = None


class RuleEngine:
    """Runs the detectors each sample; keeps persistence and hold per (detector, key). Not thread-safe: the sampler
    thread owns it."""

    def __init__(self, detectors: Sequence[Detector] | None = None):
        self.detectors: tuple[Detector, ...] = tuple(detectors if detectors is not None else default_detectors())
        self._tracks: dict[tuple[str, str], _Track] = {}

    def active(self) -> tuple[Assessment, ...]:
        return tuple(t.active for t in self._tracks.values() if t.active is not None)

    def evaluate(self, inp: RuleInput) -> tuple[tuple[Assessment, ...], tuple[DetectorStatus, ...],
                                                 list[tuple[str, Assessment]]]:
        """(open assessments, detector statuses, transitions [("raised"|"updated"|"resolved", assessment)])."""
        statuses: list[DetectorStatus] = []
        transitions: list[tuple[str, Assessment]] = []
        for d in self.detectors:
            try:
                on, note = d.status(inp)
                hits = d.check(inp) if on else []
            except Exception as exc:  # noqa: BLE001 - one broken rule never stops the others or the sampler
                on, note, hits = False, f"error: {type(exc).__name__}: {exc}", []
            statuses.append(DetectorStatus(d.name, d.version, d.subsystem, d.title, on, note))
            hit_keys = set()
            for h in hits:
                hit_keys.add(h.key)
                tr = self._tracks.setdefault((d.name, h.key), _Track())
                tr.streak += 1
                tr.last_hit = inp.now
                if tr.active is not None:
                    ev = tuple(dict.fromkeys(tr.active.evidence_event_ids + h.evidence))[-MAX_EVIDENCE:]
                    tr.active = replace(tr.active, last_seen=inp.now, detail=h.detail, title=h.title,
                                        evidence_event_ids=ev, data=h.data, baseline=h.baseline or tr.active.baseline)
                elif tr.streak >= d.raise_after:
                    tr.active = Assessment(
                        new_id(), d.name, d.version, h.key, h.subsystem, h.title, h.detail,
                        Provenance.DEMO if h.simulated else Provenance.INFERRED, inp.now, inp.now, HealthState.WATCH,
                        "medium", h.evidence[-MAX_EVIDENCE:], capabilities_affected_by(h.subsystem), h.baseline,
                        h.limit, d.hypotheses, d.checks, h.data)
                    transitions.append(("raised", tr.active))
            for (name, key), tr in self._tracks.items():
                if name != d.name or key in hit_keys:
                    continue
                tr.streak = 0
                if tr.active is None:
                    continue
                quiet = (inp.now - tr.last_hit).total_seconds() if tr.last_hit else float("inf")
                if not on or quiet >= d.clear_after_s:
                    transitions.append(("resolved", replace(tr.active, last_seen=inp.now,
                                                            detail=tr.active.detail if on else
                                                            f"{tr.active.detail} (rule inactive: {note})")))
                    tr.active = None
        return self.active(), tuple(statuses), transitions


# ====================================================================================================== helpers
def _within(events: Iterable[HealthEvent], now: datetime, window_s: float, kinds: tuple[str, ...]) -> list[HealthEvent]:
    return [e for e in events if e.kind in kinds and 0 <= (now - e.observed_at).total_seconds() <= window_s]


def _devices(inp: RuleInput, *subsystems: str) -> list[str]:
    return [d for s in subsystems for d in inp.view.devices.get(s, ())]


# ====================================================================================================== rules
class CommandErrorBurst(Detector):
    name, version, subsystem = "comm.error_burst", "1", "communications"
    title = "Burst of failed or timed-out commands to one device"
    window_s, threshold = 60.0, 3
    clear_after_s = 120.0
    hypotheses = ("intermittent link (cable, connector, USB or serial)", "device busy, faulted or refusing commands")
    checks = ("Read the failed commands in the journal (their messages say what the device answered)",
              "Check the device's cable and connector seating")

    def check(self, inp: RuleInput) -> list[Hit]:
        by: dict[str, list[HealthEvent]] = {}
        for e in _within(inp.events, inp.now, self.window_s, ("command.failed", "command.timed_out")):
            if e.device_id:
                by.setdefault(e.device_id, []).append(e)
        out = []
        for did, evs in by.items():
            if len(evs) < self.threshold:
                continue
            tos = sum(e.kind == "command.timed_out" for e in evs)
            out.append(Hit(did, inp.subsystem_of(did),
                           f"{did}: {len(evs)} failed or timed-out commands within {self.window_s:.0f} s",
                           f"{len(evs) - tos} failed, {tos} timed out; last: {evs[-1].message}",
                           tuple(e.event_id for e in evs),
                           limit=f"{self.threshold} within {self.window_s:.0f} s ({PROVISIONAL})",
                           simulated=inp.simulated(did), data={"failed": len(evs) - tos, "timed_out": tos}))
        return out


class LinkErrors(Detector):
    name, version, subsystem = "comm.link_errors", "1", "communications"
    title = "Repeated link errors on one link"
    window_s, threshold = 600.0, 3
    hypotheses = ("loose or damaged cable or connector", "USB hub or port power", "electrical noise on the link")
    checks = ("Check the link's cable, connector and hub", "Compare with the times of other events in the journal")

    def check(self, inp: RuleInput) -> list[Hit]:
        out = []
        for link in inp.view.links:
            errs = [(t, m) for t, m in inp.view.link_errors.get(link.key, ())
                    if 0 <= (inp.now - t).total_seconds() <= self.window_s]
            if len(errs) < self.threshold:
                continue
            ev = tuple(e.event_id for e in inp.events if e.kind == "device.condition" and e.device_id in link.devices
                       and 0 <= (inp.now - e.observed_at).total_seconds() <= self.window_s)
            out.append(Hit(link.key, "communications", f"{link.title}: {len(errs)} link errors within "
                           f"{self.window_s / 60:.0f} min", f"last: {errs[-1][1]}", ev,
                           limit=f"{self.threshold} within {self.window_s / 60:.0f} min ({PROVISIONAL})",
                           simulated=all(inp.simulated(d) for d in link.devices) if link.devices else False,
                           data={"errors": len(errs)}))
        return out


class Reconnects(Detector):
    name, version, subsystem = "comm.reconnects", "1", "communications"
    title = "Device reconnected itself repeatedly"
    window_s, threshold = 1800.0, 2
    clear_after_s = 60.0
    hypotheses = ("USB drop (cable, hub or port power)", "the device restarting (power or firmware)")
    checks = ("Check the USB cable and hub", "Look for the drop and reconnect reasons in the journal")

    def status(self, inp: RuleInput) -> tuple[bool, str]:
        if not inp.series.keys("backend.reconnects."):
            return False, "inactive: no backend in this session counts its reconnects"
        return True, ""

    def check(self, inp: RuleInput) -> list[Hit]:
        out = []
        for key in inp.series.keys("backend.reconnects."):
            did = key.removeprefix("backend.reconnects.")
            n = inp.series.delta(key, inp.t - self.window_s)
            if n is None or n < self.threshold:
                continue
            ev = tuple(e.event_id for e in inp.events if e.kind == "device.condition" and e.device_id == did
                       and 0 <= (inp.now - e.observed_at).total_seconds() <= self.window_s)
            out.append(Hit(did, inp.subsystem_of(did), f"{did} reconnected {n:.0f} times within "
                           f"{self.window_s / 60:.0f} min", "counted by the backend's own reconnect watcher (ADR-0066)",
                           ev, limit=f"{self.threshold} within {self.window_s / 60:.0f} min ({PROVISIONAL})",
                           simulated=inp.simulated(did), data={"reconnects": n}))
        return out


class CameraIncompleteFrames(Detector):
    name, version, subsystem = "camera.incomplete_frames", "1", "mono_camera"
    title = "Rising share of incomplete or skipped camera frames"
    window_s, ratio, min_frames = 60.0, 0.05, 20
    raise_after, clear_after_s = 3, 60.0
    hypotheses = ("USB bandwidth (hub shared with another device, cable)", "camera or driver under load")
    checks = ("Keep the camera alone on its USB port", "Compare with the journal's acquisition events")

    @staticmethod
    def _bad_key(inp: RuleInput, did: str) -> str | None:
        for k in (f"backend.incomplete_frames.{did}", f"backend.skipped_grabs.{did}"):
            if inp.series.last(k) is not None:
                return k
        return None

    def status(self, inp: RuleInput) -> tuple[bool, str]:
        cams = _devices(inp, "mono_camera", "colour_camera")
        if not any(self._bad_key(inp, d) for d in cams):
            return False, "inactive: no camera backend in this session counts incomplete frames"
        return True, ""

    def check(self, inp: RuleInput) -> list[Hit]:
        out = []
        since = inp.t - self.window_s
        for did in _devices(inp, "mono_camera", "colour_camera"):
            bad_key = self._bad_key(inp, did)
            if bad_key is None:
                continue
            bad = inp.series.delta(bad_key, since)
            good = inp.series.delta(f"backend.frames_produced.{did}", since)
            if bad is None or good is None or bad + good < self.min_frames:
                continue
            share = bad / (bad + good)
            if share < self.ratio:
                continue
            what = "incomplete" if "incomplete" in bad_key else "skipped"
            out.append(Hit(did, inp.subsystem_of(did), f"{did}: {share:.0%} of frames {what} in the last "
                           f"{self.window_s:.0f} s", f"{bad:.0f} {what}, {good:.0f} delivered; the backend skips them, "
                           "no delivered frame is affected", (), limit=f"{self.ratio:.0%} of at least "
                           f"{self.min_frames} frames in {self.window_s:.0f} s ({PROVISIONAL})",
                           simulated=inp.simulated(did), data={"bad": bad, "good": good, "share": share}))
        return out


AXES = ("X", "Y")


def following_metric(axis: str) -> str:
    return f"xy_stage.following_error.{axis}"


class FollowingError(Detector):
    """The kit's "gradual XY drift" example, below the StageMonitor's own limit: the rolling median of |APOS - CPOS|
    at rest creeping above its baseline. The StageMonitor warns once one reading is more than 3 counts from the
    command (MOVE_DRIFT_MAX_COUNTS) and locks on drift; this rule only says WATCH, earlier, from a trend."""

    name, version, subsystem = "stage.following_error", "1", "xy_stage"
    title = "X/Y offset at rest creeping above its baseline"
    window_s, min_points, min_shift_counts = 120.0, 30, 1.0
    raise_after, clear_after_s = 5, 60.0
    hypotheses = ("mechanical load or friction on the axis", "drive tuning or holding current")
    checks = ("Review the offset trend for the axis", "Inspect the stage area for obstructions")

    def status(self, inp: RuleInput) -> tuple[bool, str]:
        notes = []
        for ax in AXES:
            if inp.series.last(following_metric(ax)) is None:
                continue
            b, why = inp.baselines.valid(following_metric(ax), "idle", inp.fingerprint)
            if b is not None:
                return True, ""
            notes.append(f"{ax}: {why}")
        if not notes:
            return False, "inactive: no X/Y encoder and command counts in this session"
        return False, "inactive: " + "; ".join(notes)

    def check(self, inp: RuleInput) -> list[Hit]:
        out = []
        stage = next(iter(inp.view.devices.get("xy_stage", ())), "")
        for ax in AXES:
            key = following_metric(ax)
            b, _ = inp.baselines.valid(key, "idle", inp.fingerprint)
            pts = inp.series.points(key, since=inp.t - self.window_s, regime="idle")
            if b is None or len(pts) < self.min_points:
                continue
            m = median([v for _, v in pts])
            if m <= b.p95 or m - b.median < self.min_shift_counts:
                continue
            z = robust_z(m, b.median, b.mad, 1.0)
            out.append(Hit(ax, "xy_stage", f"{ax} sits {m:.1f} counts from its command at rest (median of "
                           f"{len(pts)} readings; baseline median {b.median:.1f}, p95 {b.p95:.1f})",
                           f"{z:.1f} robust deviations above the baseline; the drift monitor's own limit is 3 counts",
                           (), b.ref(), f"rolling median above the baseline p95 and at least "
                           f"{self.min_shift_counts:g} count above its median",
                           simulated=inp.simulated(stage) or b.source == "simulator",
                           data={"median": m, "n": len(pts), "z": z, "baseline_version": b.version}))
        return out


class WarningRecurrence(Detector):
    name, version, subsystem = "stage.warning_recurrence", "1", "xy_stage"
    title = "Drift-monitor warnings keep coming back"
    window_s, threshold = 1800.0, 2
    hypotheses = ("an axis creeping at rest (mechanical or holding current)", "landing short of the target")
    checks = ("Read the warnings in the journal and compare their axes",
              "Watch the axis at rest; the drift monitor locks it if it moves further")

    def check(self, inp: RuleInput) -> list[Hit]:
        evs = _within(inp.events, inp.now, self.window_s, ("stage_monitor.warning",))
        if len(evs) < self.threshold:
            return []
        return [Hit("X/Y", "xy_stage", f"{len(evs)} drift-monitor warnings within {self.window_s / 60:.0f} min",
                    f"last: {evs[-1].message}", tuple(e.event_id for e in evs),
                    limit=f"{self.threshold} within {self.window_s / 60:.0f} min ({PROVISIONAL})",
                    simulated=evs[-1].provenance is Provenance.DEMO, data={"warnings": len(evs)})]


class LandingReapproach(Detector):
    name, version, subsystem = "stage.landing_reapproach", "1", "xy_stage"
    title = "Moves often land short and need a re-approach"
    window_s, threshold = 1800.0, 3
    hypotheses = ("friction or load near part of the travel", "the axis's tight spot (Y, ~1.9 mm from home)")
    checks = ("Note where the re-approaches happened (the StageMonitor lists the positions)",
              "Compare with the drift monitor's landing warnings")

    def check(self, inp: RuleInput) -> list[Hit]:
        out = []
        for ax in AXES:
            n = inp.series.delta(f"xy_stage.reapproaches.{ax}", inp.t - self.window_s)
            if n is None or n < self.threshold:
                continue
            stage = next(iter(inp.view.devices.get("xy_stage", ())), "")
            out.append(Hit(ax, "xy_stage", f"{ax}: {n:.0f} automatic re-approaches within "
                           f"{self.window_s / 60:.0f} min", "the move landed off its target and the service "
                           "re-approached it (ADR-0061)", (),
                           limit=f"{self.threshold} within {self.window_s / 60:.0f} min ({PROVISIONAL})",
                           simulated=inp.simulated(stage), data={"reapproaches": n}))
        return out


class StorageTimeToFull(Detector):
    name, version, subsystem = "storage.time_to_full", "1", "storage"
    title = "Data drive filling fast"
    window_s, horizon_s, min_points, min_span_s = 600.0, 7200.0, 5, 120.0
    raise_after, clear_after_s = 3, 120.0
    hypotheses = ("an acquisition writing more than the drive can hold", "another program filling the drive")
    checks = ("Check the free space before the next long acquisition", "Move finished datasets off the data drive")

    def check(self, inp: RuleInput) -> list[Hit]:
        out = []
        for key in inp.series.keys("storage.free_bytes."):
            pts = inp.series.points(key, since=inp.t - self.window_s)
            if len(pts) < self.min_points or pts[-1][0] - pts[0][0] < self.min_span_s:
                continue
            slope = linear_slope(pts)
            if slope is None or slope >= 0:
                continue
            free = pts[-1][1]
            eta = free / -slope
            if eta >= self.horizon_s:
                continue
            which = key.removeprefix("storage.free_bytes.")
            out.append(Hit(which, "storage", f"{which} drive full in about {eta / 60:.0f} min at the current rate",
                           f"{free / 1e9:.1f} GB free, falling {-slope * 60 / 1e6:.0f} MB/min over the last "
                           f"{(pts[-1][0] - pts[0][0]) / 60:.0f} min", (),
                           limit=f"full within {self.horizon_s / 3600:.0f} h ({PROVISIONAL})",
                           data={"free_bytes": free, "bytes_per_s": slope, "eta_s": eta}))
        return out


def default_detectors() -> list[Detector]:
    return [CommandErrorBurst(), LinkErrors(), Reconnects(), CameraIncompleteFrames(), FollowingError(),
            WarningRecurrence(), LandingReapproach(), StorageTimeToFull()]
