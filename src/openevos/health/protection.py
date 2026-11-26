"""Capability-scoped protection (ADR-0070, Instrument Health Phase 2): policies, proposals, verification.

The operator chose (2026-11-26) "ask, then restrict": a policy turns a rule's open WATCH into a *proposal* on the
Health page. Nothing is restricted until the operator confirms it. Then the request goes to the SafetyManager's
registry (`openevos.safety.restrictions`), which enforces it. Health checks each restriction's verification every
sample. When the verification passes, the restriction is lifted through the registry, with the verification recorded.

Policies are few and narrow on purpose. Each restricts one capability and leaves the rest available. For example, with
multi-position runs restricted, stationary imaging and manual moves still work (the kit's own example). A policy has no
verification it cannot carry out without moving the instrument; rules whose release would need a test move (the
drift-warning recurrence, landing re-approaches, command bursts) stay WATCH only.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import uuid4

from openevos.health.model import Assessment
from openevos.health.rules import MetricSeries, following_metric
from openevos.health.stats import BaselineStore, linear_slope, median
from openevos.safety.restrictions import DATA_SAVING, MULTI_POSITION, Restriction, imaging

#: a new restriction for the same policy and key this soon after the last one was released: call for service
RECURRENCE_S = 24 * 3600.0


@dataclass(frozen=True)
class Verification:
    passed: bool
    summary: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {"passed": self.passed, "summary": self.summary, "data": self.data}


@dataclass(frozen=True)
class VerifyInput:
    now: float  # epoch seconds
    activated_at: float  # epoch seconds
    key: str
    series: MetricSeries
    baselines: BaselineStore
    fingerprint: dict[str, str]


@dataclass(frozen=True)
class Policy:
    policy_id: str
    version: str
    detector: str
    #: what is restricted, in words, for a key
    title: Callable[[str], str]
    #: what stays available, in words (shown when asking the operator)
    keeps: str
    #: run capabilities and commands restricted, for a key
    capabilities: Callable[[str], tuple[str, ...]]
    commands: Callable[[str], tuple[tuple[str, str], ...]]
    release_conditions: tuple[str, ...]
    verify: Callable[[VerifyInput], Verification]
    #: only these keys (None: every key the detector raises)
    keys: tuple[str, ...] | None = None


# ====================================================================================================== verifications
FOLLOWING_VERIFY_S, FOLLOWING_VERIFY_POINTS = 300.0, 30


def verify_following(inp: VerifyInput) -> Verification:
    metric = following_metric(inp.key)
    b, why = inp.baselines.valid(metric, "idle", inp.fingerprint)
    if b is None:
        return Verification(False, f"cannot verify: {why}")
    since = max(inp.activated_at, inp.now - FOLLOWING_VERIFY_S)
    pts = inp.series.points(metric, since=since, regime="idle")
    watched = inp.now - inp.activated_at
    if watched < FOLLOWING_VERIFY_S or len(pts) < FOLLOWING_VERIFY_POINTS:
        span = min(watched, FOLLOWING_VERIFY_S)
        return Verification(False, f"watching: {len(pts)} readings at rest over {span:.0f} s "
                                   f"(needs {FOLLOWING_VERIFY_POINTS} over {FOLLOWING_VERIFY_S:.0f} s)")
    m = median([v for _, v in pts])
    data = {"median": m, "n": len(pts), "baseline": b.ref(), "p95": b.p95}
    if m > b.p95:
        return Verification(False, f"{inp.key} still {m:.1f} counts from its command at rest (baseline p95 "
                                   f"{b.p95:.1f})", data)
    return Verification(True, f"{inp.key} back within its baseline: median {m:.1f} counts over {len(pts)} readings "
                              f"(baseline v{b.version} p95 {b.p95:.1f})", data)


CAMERA_VERIFY_S, CAMERA_VERIFY_FRAMES, CAMERA_VERIFY_RATIO = 60.0, 200, 0.01


def verify_camera(inp: VerifyInput) -> Verification:
    since = max(inp.activated_at, inp.now - CAMERA_VERIFY_S)
    bad = None
    for k in (f"backend.incomplete_frames.{inp.key}", f"backend.skipped_grabs.{inp.key}"):
        if inp.series.last(k) is not None:
            bad = inp.series.delta(k, since)
            break
    good = inp.series.delta(f"backend.frames_produced.{inp.key}", since)
    if bad is None or good is None or bad + good < CAMERA_VERIFY_FRAMES:
        n = 0 if bad is None or good is None else bad + good
        return Verification(False, f"watching: {n:.0f} frames in the last {CAMERA_VERIFY_S:.0f} s (needs "
                                   f"{CAMERA_VERIFY_FRAMES}; start live view on this camera to verify)")
    share = bad / (bad + good)
    data = {"bad": bad, "good": good, "share": share}
    if share >= CAMERA_VERIFY_RATIO:
        return Verification(False, f"still {share:.1%} of frames incomplete (needs under {CAMERA_VERIFY_RATIO:.0%})",
                            data)
    return Verification(True, f"{share:.1%} of {bad + good:.0f} frames incomplete over {CAMERA_VERIFY_S:.0f} s", data)


STORAGE_VERIFY_S, STORAGE_HORIZON_S = 300.0, 6 * 3600.0


def verify_storage(inp: VerifyInput) -> Verification:
    key = f"storage.free_bytes.{inp.key}"
    since = max(inp.activated_at, inp.now - 2 * STORAGE_VERIFY_S)
    pts = inp.series.points(key, since=since)
    if len(pts) < 5 or pts[-1][0] - pts[0][0] < STORAGE_VERIFY_S:
        return Verification(False, f"watching: {len(pts)} free-space readings (needs 5 over "
                                   f"{STORAGE_VERIFY_S / 60:.0f} min)")
    slope = linear_slope(pts) or 0.0
    free = pts[-1][1]
    eta = free / -slope if slope < 0 else None
    data = {"free_bytes": free, "bytes_per_s": slope, "eta_s": eta}
    if eta is not None and eta < STORAGE_HORIZON_S:
        return Verification(False, f"{inp.key} drive still projected full in about {eta / 60:.0f} min", data)
    return Verification(True, f"{inp.key} drive: {free / 1e9:.1f} GB free, "
                              + ("not filling" if eta is None else f"full in about {eta / 3600:.0f} h at this rate"),
                        data)


# ====================================================================================================== policies
POLICIES: tuple[Policy, ...] = (
    Policy("xy.multi_position.following_error", "1", "stage.following_error",
           lambda k: "Multi-position runs (Scan, Time-lapse, Multi-D with several positions)",
           "Stationary imaging, Z-stacks at one position and manual stage moves stay available",
           lambda k: (MULTI_POSITION,), lambda k: (),
           ((f"the axis offset at rest is back within its baseline (rolling median at or below the baseline p95, at "
            f"least {FOLLOWING_VERIFY_POINTS} readings over {FOLLOWING_VERIFY_S / 60:.0f} min)"),
            "or the operator lifts it"),
           verify_following),
    Policy("camera.capture.incomplete_frames", "1", "camera.incomplete_frames",
           lambda k: f"Image capture with camera {k} (single captures and runs)",
           "Live view stays on, so the camera can be watched; the other camera stays available",
           lambda k: (imaging(k),), lambda k: ((k, "camera.snap"),),
           ((f"under {CAMERA_VERIFY_RATIO:.0%} of at least {CAMERA_VERIFY_FRAMES} frames incomplete in "
            f"{CAMERA_VERIFY_S:.0f} s of live view"), "or the operator lifts it"),
           verify_camera),
    Policy("storage.data_saving.time_to_full", "1", "storage.time_to_full",
           lambda k: "Starting new runs that save data (Scan, Time-lapse, Multi-D, Z-stack)",
           "Live view, single captures and moves stay available; a run already going is not cut short",
           lambda k: (DATA_SAVING,), lambda k: (),
           ((f"the data drive is no longer projected to fill within {STORAGE_HORIZON_S / 3600:.0f} h, over "
            f"{STORAGE_VERIFY_S / 60:.0f} min of readings"), "or the operator lifts it"),
           verify_storage, keys=("data",)),
)
POLICY_BY_ID = {p.policy_id: p for p in POLICIES}


def policy_for(a: Assessment) -> Policy | None:
    for p in POLICIES:
        if p.detector == a.detector and (p.keys is None or a.key in p.keys):
            return p
    return None


@dataclass(frozen=True)
class Proposal:
    """A restriction Health proposes for an open WATCH. Nothing happens until the operator confirms it."""

    proposal_id: str  # "<policy id>:<key>"
    policy: Policy
    assessment: Assessment
    title: str
    capabilities: tuple[str, ...]
    commands: tuple[tuple[str, str], ...]
    #: runs going now that need what it restricts: "Scan", "Multi-D" ...
    runs: tuple[str, ...] = ()
    #: the same policy and key were restricted and released within RECURRENCE_S
    recurring: bool = False

    def restriction(self, now: datetime) -> Restriction:
        a = self.assessment
        return Restriction(
            restriction_id=f"r-{now.strftime('%Y%m%dT%H%M%S')}-{uuid4().hex[:8]}",
            policy_id=self.policy.policy_id, policy_version=self.policy.version, title=self.title,
            reason=f"{a.title}: {a.detail}", capabilities=self.capabilities, commands=self.commands,
            trigger={"detector": a.detector, "detector_version": a.detector_version, "key": a.key,
                     "assessment_id": a.assessment_id, "evidence_event_ids": list(a.evidence_event_ids),
                     "limit": a.limit, "baseline": a.baseline, "provenance": a.provenance.value,
                     "recurring": self.recurring},
            release_conditions=self.policy.release_conditions, requested_at=now.isoformat())


# ====================================================================================================== mapping
#: run capability -> the Health page's capability keys
def page_capabilities(cap: str, subsystem_of: Callable[[str], str]) -> tuple[str, ...]:
    if cap == MULTI_POSITION:
        return ("multi_position",)
    if cap == DATA_SAVING:
        return ("data_saving",)
    if cap.startswith("imaging:"):
        sub = subsystem_of(cap.removeprefix("imaging:"))
        return ("colour_imaging",) if sub == "colour_camera" else ("mono_imaging", "fluorescence_imaging")
    return ()
