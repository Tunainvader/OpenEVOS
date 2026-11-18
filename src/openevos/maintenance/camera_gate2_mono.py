"""Camera Gate 2 (mono): RAM-only settings, 16-bit dark frames, verified restore (ADR-0036).

Spec: docs/evidence/camera_gate2_mono_settings_spec.md.

Sequence (one attempt; any failure -> restore -> stop):
  1 connect + identity; 2 snapshot (compared with the Gate 1 record); 3 Format7 mode 0 1296x964 MONO16
  (validated, SDK-recommended packet size); 4 auto exposure off, gain 0 dB, brightness 2 %;
  5 shutter 1/10/50 ms: discard 1 frame, keep 3; 6 restore power-up state (always); 7 verify; 8 close.

Setters bound in addition to Gate 1's read-only set: fc2ValidateFormat7Settings,
fc2SetFormat7ConfigurationPacket, fc2SetProperty, fc2SetVideoModeAndFrameRate. No memory-channel
save/load, register, configuration, trigger, strobe, LUT, bus-reset or firmware call is bound.
All values are constants below.
"""

from __future__ import annotations

import argparse
import json
import math
import platform
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from openevos.backends.flycapture2.sdk import (  # noqa: F401 - re-exported for the gate tests
    BRIGHTNESS_PCT,
    COMPARED,
    F7_FORMAT,
    F7_HEIGHT,
    F7_MODE,
    F7_WIDTH,
    FC2_IMAGE_CONSISTENCY_ERROR,
    PACKET_FRACTION,
    SETTABLE,
    SETTER_BOUND,
    TOL,
    FlyCapture2SettingsBackend,
    Format7PacketInfo,
    Gate2Error,
    compare_state,
    restore_state,
)
from openevos.maintenance import camera_gate1 as g1

SPEC = "docs/evidence/camera_gate2_mono_settings_spec.md; ADR-0036"
EVIDENCE_PREFIX = "camera-gate2-mono"
ROLE = g1.ROLES["mono"]
CONFIRM_PHRASE = "SET CAMERA MONO 1E10:2005 RAM ONLY AND RESTORE WITH LED OFF"

GAIN_DB = 0.0
SHUTTERS_MS = (1.0, 10.0, 50.0)
FRAMES_KEPT, FRAMES_DISCARDED = 3, 1
GRAB_TIMEOUT_S = 5.0
MAX_SKIPPED_PER_EXPOSURE = 3


# -- analysis --------------------------------------------------------------------------------
def dark_stats(frames: list[np.ndarray]) -> dict[str, Any]:
    st = np.stack(frames).astype(np.float64)
    first = st[0]
    vals = np.unique(frames[0])
    steps = np.diff(vals)
    step = int(np.gcd.reduce(steps.astype(np.int64))) if steps.size else 0
    mean, sigma = float(first.mean()), float(first.std())
    out = {"shape": list(frames[0].shape), "dtype": str(frames[0].dtype), "n": len(frames),
           "mean": float(st.mean()), "median": float(np.median(first)), "spatial_std": sigma,
           "min": float(st.min()), "max": float(st.max()),
           "p0_1": float(np.percentile(first, 0.1)), "p99_9": float(np.percentile(first, 99.9)),
           "saturated": int((st >= 65535).sum()), "histogram_step": step,
           "column_mean_std": float(first.mean(axis=0).std()), "row_mean_std": float(first.mean(axis=1).std()),
           "hot_candidates_10sigma": int((first > mean + 10 * sigma).sum()) if sigma > 0 else 0}
    if len(frames) >= 2:
        out["temporal_noise"] = float((st[0] - st[1]).std() / math.sqrt(2))
    return out


def find_gate1_record(evidence_dir: Path, serial: int) -> tuple[Path | None, dict | None]:
    for p in sorted(evidence_dir.glob("camera-gate1-mono-*.json"), reverse=True):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if d.get("identity", {}).get("serial") == serial and "modes" in d.get("steps_done", []):
            return p, d
    return None, None


# -- gate ------------------------------------------------------------------------------------
@dataclass
class Gate2Result:
    outcome: str  # dry_run | refused | stopped | clipped | complete
    message: str
    started_utc: str
    host: str
    gate1_record: str | None = None
    identity: dict = field(default_factory=dict)
    snapshot: dict = field(default_factory=dict)
    settings: list[dict] = field(default_factory=list)
    exposures: dict = field(default_factory=dict)
    restore_diffs: list[str] | None = None
    restore_verified: bool = False
    frame_files: list[str] = field(default_factory=list)
    opened: bool = False
    passed: bool = False
    spec: str = SPEC

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, ensure_ascii=False, default=str)


def _check(res: Gate2Result, b: FlyCapture2SettingsBackend, name: str, want: dict[str, Any]) -> None:
    got = b.get_prop(name)
    res.settings.append({"property": name, "requested": want, "read_back": got})
    for k in ("on", "auto"):
        if k in want and got[k] != want[k]:
            raise Gate2Error(f"{name}.{k} read back {got[k]}, requested {want[k]}")
    if "abs_value" in want:
        tol = TOL[name](want["abs_value"])
        if abs(got["abs_value"] - want["abs_value"]) > tol:
            raise Gate2Error(f"{name} read back {got['abs_value']:.4g}, requested {want['abs_value']} (±{tol:.3g})")


def run_gate2(pnp: g1.PnpEnumerator, b: FlyCapture2SettingsBackend, *, evidence_dir: Path, send: bool = False,
              confirm: str = "") -> Gate2Result:
    res = Gate2Result("refused", "", datetime.now(timezone.utc).isoformat(timespec="milliseconds"), platform.node())
    devs = pnp.present(ROLE.vid, ROLE.pid)
    if len(devs) != 1 or devs[0].problem_code != 0:
        res.message = f"need exactly one {ROLE.vid_pid} with a working driver, found {[asdict(d) for d in devs]}; nothing opened"  # noqa: E501
        return res
    if not b.sdk_available() or b.count_cameras() != 1:
        res.message = "FlyCapture2 SDK missing or does not see exactly one camera; nothing opened"
        return res
    if not list(evidence_dir.glob("camera-gate1-mono-*.json")):
        res.message = "no Camera Gate 1 record in the evidence folder; nothing opened"
        return res
    if not send:
        res.outcome = "dry_run"
        res.message = (f"dry run: would set Format7 {F7_WIDTH}x{F7_HEIGHT} MONO16, gain {GAIN_DB} dB, brightness "
                       f"{BRIGHTNESS_PCT} %, shutter {SHUTTERS_MS} ms x{FRAMES_KEPT} frames, then restore; nothing opened")  # noqa: E501
        return res
    if confirm != CONFIRM_PHRASE:
        res.message = f"confirmation phrase missing or wrong (expected {CONFIRM_PHRASE!r}); nothing opened"
        return res

    res.opened = True
    stage = "connect"
    snap: dict[str, Any] | None = None
    try:
        b.connect()
        stage = "identity"
        res.identity = b.identity()
        rec_path, rec = find_gate1_record(evidence_dir, res.identity["serial"])
        if rec is None:
            raise Gate2Error("no completed Gate 1 record for this camera serial")
        res.gate1_record = rec_path.name
        stage = "snapshot"
        snap = b.snapshot()
        res.snapshot = snap
        diffs = compare_state(rec["modes"], snap)
        if diffs:
            snap = None  # nothing changed yet: no restore needed
            raise Gate2Error("live state differs from the Gate 1 record: " + "; ".join(diffs))
        stage = "format7"
        res.settings.append({"format7": b.set_format7_mono16_full()})
        stage = "manual settings"
        b.set_prop("auto_exposure", on=False, auto=False)
        _check(res, b, "auto_exposure", {"on": False, "auto": False})
        b.set_prop("gain", on=True, auto=False, abs_value=GAIN_DB)
        _check(res, b, "gain", {"auto": False, "abs_value": GAIN_DB})
        b.set_prop("brightness", on=True, auto=False, abs_value=BRIGHTNESS_PCT)
        _check(res, b, "brightness", {"abs_value": BRIGHTNESS_PCT})
        for ms in SHUTTERS_MS:
            stage = f"dark frames {ms} ms"
            b.set_prop("shutter", on=True, auto=False, abs_value=ms)
            _check(res, b, "shutter", {"auto": False, "abs_value": ms})
            frames = b.grab_series(FRAMES_DISCARDED, FRAMES_KEPT, GRAB_TIMEOUT_S)
            if any(f.shape != (F7_HEIGHT, F7_WIDTH) or f.dtype != np.uint16 for f in frames):
                raise Gate2Error(f"frames are {frames[0].shape} {frames[0].dtype}, expected ({F7_HEIGHT}, {F7_WIDTH}) uint16")  # noqa: E501
            stats = dark_stats(frames)
            stats["incomplete_frames_skipped"] = b.skipped
            res.exposures[f"{ms:g}ms"] = stats
            evidence_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            fpath = evidence_dir / f"{EVIDENCE_PREFIX}-{stamp}-{ms:g}ms.npy"
            np.save(fpath, np.stack(frames))
            res.frame_files.append(fpath.name)
            if stats["mean"] > 0.25 * 65535:
                raise Gate2Error(f"frames at {ms} ms are bright (mean {stats['mean']:.0f}): light leak")
        stage = "done"
    except Exception as exc:  # noqa: BLE001 - restore, report, never retry
        res.outcome = "stopped"
        res.message = f"{stage} failed: {exc}; not retried"

    if snap is not None:
        try:
            restore_state(b, snap)
            after = b.snapshot()
            res.restore_diffs = compare_state(snap, after)
            res.restore_verified = not res.restore_diffs
        except Exception as exc:  # noqa: BLE001
            res.restore_diffs = [f"restore failed: {exc}"]
        if not res.restore_verified:
            res.message += ("; RESTORE NOT VERIFIED: power-cycle the instrument, then re-run Camera Gate 1. "
                            f"Differences: {res.restore_diffs}")
    try:
        b.close()
    except Exception as exc:  # noqa: BLE001
        res.message += f"; close failed: {exc}"

    if res.outcome == "stopped":
        return res
    clipped = [k for k, s in res.exposures.items() if s["p0_1"] <= 0]
    if not res.restore_verified:
        res.outcome = "stopped"
        res.message = "frames read" + res.message
    elif clipped:
        res.outcome = "clipped"
        res.message = f"dark frames clip at 0 at {clipped} with brightness {BRIGHTNESS_PCT} %; restore verified"
    elif any(s["saturated"] for s in res.exposures.values()):
        res.outcome = "stopped"
        res.message = "saturated pixels in a dark frame; restore verified"
    else:
        res.outcome, res.passed = "complete", True
        e = res.exposures
        res.message = ("settings read back, unclipped dark frames, restore verified. "
                       + ", ".join(f"{k}: mean {s['mean']:.1f}, noise {s.get('temporal_noise', 0):.1f}" for k, s in e.items()))  # noqa: E501
    return res


def write_evidence(result: Gate2Result, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    path = out_dir / f"{EVIDENCE_PREFIX}-{stamp}-{result.outcome}.json"
    path.write_text(result.to_json(), encoding="utf-8")
    return path


def main(argv: list[str] | None = None, pnp: g1.PnpEnumerator | None = None,
         backend: FlyCapture2SettingsBackend | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m openevos.maintenance.camera_gate2_mono",
                                description="Camera Gate 2 (mono): RAM-only settings, 16-bit dark frames, restore. "
                                            "Dry run by default.")
    p.add_argument("--send", action="store_true")
    p.add_argument("--confirm", default="", help=f"must be exactly: {CONFIRM_PHRASE}")
    p.add_argument("--evidence-dir", type=Path, default=Path.cwd() / "openevos-scans")
    a = p.parse_args(argv)
    if pnp is None:
        if sys.platform != "win32":
            print("This tool needs Windows.", file=sys.stderr)
            return 2
        pnp = g1.WindowsPnp()
    backend = backend or FlyCapture2SettingsBackend()
    res = run_gate2(pnp, backend, evidence_dir=a.evidence_dir, send=a.send, confirm=a.confirm)
    print(f"[{res.outcome}] {res.message}")
    if res.opened or res.outcome == "dry_run":
        print(f"evidence: {write_evidence(res, a.evidence_dir)}")
    return 0 if res.outcome == "dry_run" or res.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
