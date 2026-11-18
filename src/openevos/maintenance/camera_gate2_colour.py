"""Camera Gate 2 (colour): RAM-only settings, raw Bayer dark frames, restore by exit + re-init check (ADR-0038).

Spec: docs/evidence/camera_gate2_colour_settings_spec.md.

Sequence (one attempt; any failure -> restore -> stop):
  1 connect + identity; 2 snapshot (compared with the colour Gate 1 record); 3 first accepted raw mode of
  RAW16/RAW12/RAW10/RAW8; 4 master gain 0, black-level auto off, offset = default + 25 % of range;
  5 exposure 1/10/50 ms: discard 1 frame, keep 3; 6 restore = is_ExitCamera (always); 7 verify = fresh
  flag-free is_InitCamera, re-read, compare with the snapshot, exit.

Setters (guarded in `backends.ueye.sdk.UeyeSettingsBackend`): is_SetColorMode (raw candidates),
is_SetHardwareGain (0, -1, -1, -1), is_Blacklevel (auto off, the computed offset), is_Exposure SET
(1, 10, 50 ms). No parameter-set, EEPROM, configuration, LUT, I/O, trigger, pixel-clock, frame-rate or
area setter is bound. All values are constants.
"""

from __future__ import annotations

import argparse
import json
import math
import platform
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from openevos.backends.ueye.sdk import (
    BL_CAP_SET_AUTO,
    BL_CAP_SET_OFFSET,
    COLOR_MODES,
    EXPOSURES_MS,
    GRAB_SKIP_CODES,
    IS_AUTO_BLACKLEVEL_OFF,
    RAW_CANDIDATES,
    UeyeError,
    UeyeSettingsBackend,
    offset_target,
)
from openevos.maintenance import camera_gate1 as g1

SPEC = "docs/evidence/camera_gate2_colour_settings_spec.md; ADR-0038"
EVIDENCE_PREFIX = "camera-gate2-colour"
ROLE = g1.ROLES["colour"]
CONFIRM_PHRASE = "SET CAMERA COLOUR 1409:1465 RAM ONLY AND RESTORE WITH LED OFF"
FRAMES_KEPT, FRAMES_DISCARDED = 3, 1
GRAB_TIMEOUT_S = 5.0
MAX_SKIPPED_PER_EXPOSURE = 3
EXPOSURE_TOL_MS = 0.11  # one 0.10 ms step (Gate 1 range)
SHAPE = (1536, 2048)


class Gate2Error(RuntimeError):
    pass


# -- analysis --------------------------------------------------------------------------------
def bayer_planes(frame: np.ndarray) -> dict[str, np.ndarray]:
    """The four Bayer sites by position (row parity, column parity); colour order confirmed later with light."""
    return {"p00": frame[0::2, 0::2], "p01": frame[0::2, 1::2], "p10": frame[1::2, 0::2], "p11": frame[1::2, 1::2]}


def dark_stats(frames: list[np.ndarray], bits: int) -> dict[str, Any]:
    full = (1 << bits) - 1
    st = np.stack(frames).astype(np.float64)
    first = st[0]
    steps = np.diff(np.unique(frames[0]))
    mean, sigma = float(first.mean()), float(first.std())
    out: dict[str, Any] = {
        "shape": list(frames[0].shape), "dtype": str(frames[0].dtype), "data_bits": bits, "n": len(frames),
        "mean": float(st.mean()), "median": float(np.median(first)), "spatial_std": sigma,
        "min": float(st.min()), "max": float(st.max()),
        "p0_1": float(np.percentile(first, 0.1)), "p99_9": float(np.percentile(first, 99.9)),
        "saturated": int((st >= full).sum()),
        "histogram_step": int(np.gcd.reduce(steps.astype(np.int64))) if steps.size else 0,
        "column_mean_std": float(first.mean(axis=0).std()), "row_mean_std": float(first.mean(axis=1).std()),
        "hot_candidates_10sigma": int((first > mean + 10 * sigma).sum()) if sigma > 0 else 0,
        "planes": {k: {"mean": float(p.mean()), "std": float(p.std())} for k, p in bayer_planes(first).items()}}
    if len(frames) >= 2:
        out["temporal_noise"] = float((st[0] - st[1]).std() / math.sqrt(2))
    return out


COMPARED = ("color_mode", "aoi", "exposure_ms", "exposure_range_ms", "pixel_clock_mhz", "pixel_clock_range_mhz",
            "frame_rate_fps", "gain", "auto")


def compare_state(ref: dict[str, Any], cur: dict[str, Any], keys=COMPARED) -> list[str]:
    diffs = []
    for k in keys:
        a, b = ref.get(k), cur.get(k)
        if k == "color_mode":
            a, b = (a or {}).get("code"), (b or {}).get("code")
        if isinstance(a, float) and isinstance(b, float):
            tol = EXPOSURE_TOL_MS if k == "exposure_ms" else 0.05 if k == "frame_rate_fps" else 1e-6
            if abs(a - b) > tol:
                diffs.append(f"{k}: {a} -> {b}")
        elif isinstance(a, list) and isinstance(b, list) and len(a) == len(b) and all(
                isinstance(x, (int, float)) for x in a + b):
            if any(abs(x - y) > 1e-6 for x, y in zip(a, b, strict=False)):
                diffs.append(f"{k}: {a} -> {b}")
        elif a != b:
            diffs.append(f"{k}: {a} -> {b}")
    return diffs


def find_gate1_record(evidence_dir: Path, serial: str) -> tuple[Path | None, dict | None]:
    for p in sorted(evidence_dir.glob("camera-gate1-colour-*.json"), reverse=True):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if d.get("identity", {}).get("serial") == serial and "modes" in d.get("steps_done", []):
            return p, d
    return None, None


def app_holds_cameras() -> bool:
    """True if the OpenEVOS UI is running (it may stream the mono camera over the shared USB 2 hub)."""
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq openevos-ui.exe", "/NH"], check=False,
                         capture_output=True, text=True, timeout=30).stdout
    return "openevos-ui.exe" in out.lower()


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
    color_modes_tried: list[dict] = field(default_factory=list)
    settings: list[dict] = field(default_factory=list)
    exposures: dict = field(default_factory=dict)
    restore_snapshot: dict = field(default_factory=dict)
    restore_diffs: list[str] | None = None
    restore_verified: bool = False
    frame_files: list[str] = field(default_factory=list)
    opened: bool = False
    passed: bool = False
    spec: str = SPEC

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, ensure_ascii=False, default=str)


def grab_series(b: UeyeSettingsBackend, discard: int, keep: int) -> tuple[list[np.ndarray], int, int]:
    frames, skipped, bits = [], 0, 8
    while len(frames) < discard + keep:
        try:
            px, bits = b.grab_one(GRAB_TIMEOUT_S)
        except UeyeError as exc:
            if exc.code in GRAB_SKIP_CODES and skipped < MAX_SKIPPED_PER_EXPOSURE:
                skipped += 1
                continue
            raise
        frames.append(px)
    return frames[discard:], bits, skipped


def run_gate2(pnp: g1.PnpEnumerator, b: UeyeSettingsBackend, *, evidence_dir: Path, send: bool = False,
              confirm: str = "", busy=app_holds_cameras) -> Gate2Result:
    res = Gate2Result("refused", "", datetime.now(timezone.utc).isoformat(timespec="milliseconds"), platform.node())
    devs = pnp.present(ROLE.vid, ROLE.pid)
    if len(devs) != 1 or devs[0].problem_code != 0:
        res.message = f"need exactly one {ROLE.vid_pid} with a working driver, found {[asdict(d) for d in devs]}; nothing opened"
        return res
    if not b.sdk_available() or b.count_cameras() != 1:
        res.message = "uEye API missing or does not see exactly one camera; nothing opened"
        return res
    if not list(evidence_dir.glob("camera-gate1-colour-*.json")):
        res.message = "no colour Camera Gate 1 record in the evidence folder; nothing opened"
        return res
    if busy():
        res.message = "the OpenEVOS app is running (shared USB 2 hub with the mono camera): close it first; nothing opened"
        return res
    if not send:
        res.outcome = "dry_run"
        res.message = (f"dry run: would pick a raw mode from {RAW_CANDIDATES}, set master gain 0, black level manual "
                       f"(default + 25 % of range), exposure {EXPOSURES_MS} ms x{FRAMES_KEPT} frames, then exit and "
                       "verify by re-init; nothing opened")
        return res
    if confirm != CONFIRM_PHRASE:
        res.message = f"confirmation phrase missing or wrong (expected {CONFIRM_PHRASE!r}); nothing opened"
        return res

    res.opened = True
    stage = "connect"
    changed = False
    try:
        b.connect()
        stage = "identity"
        res.identity = b.identity()
        rec_path, rec = find_gate1_record(evidence_dir, res.identity["serial"])
        if rec is None:
            raise Gate2Error("no completed colour Gate 1 record for this camera serial")
        res.gate1_record = rec_path.name
        stage = "snapshot"
        snap = b.snapshot()
        res.snapshot = snap
        diffs = compare_state(rec["modes"], snap)
        if diffs:
            raise Gate2Error("live state differs from the Gate 1 record: " + "; ".join(diffs))
        bl = snap["blacklevel"]
        if not isinstance(bl.get("offset_range"), list) or not isinstance(bl.get("offset_default"), int):
            raise Gate2Error(f"black-level offset range/default not readable ({bl}); no setter called")
        caps = bl.get("caps") if isinstance(bl.get("caps"), int) else 0
        if not caps & BL_CAP_SET_OFFSET:
            raise Gate2Error(f"camera reports no settable black-level offset (caps {caps}); no setter called")
        mode_settable = bool(caps & BL_CAP_SET_AUTO)  # rev 2: the UI-146xLE reports caps 2 (offset only)
        offset = offset_target(bl["offset_default"], bl["offset_range"])

        stage = "colour mode"
        mode = None
        for m in RAW_CANDIDATES:
            changed = True
            ok = b.try_color_mode(m)
            res.color_modes_tried.append({"code": m, "name": COLOR_MODES[m][0], "accepted": ok})
            if ok:
                mode = m
                break
        if mode is None:
            raise Gate2Error("no raw Bayer mode accepted: " + ", ".join(COLOR_MODES[m][0] for m in RAW_CANDIDATES))
        stage = "manual settings"
        gain = b.set_master_gain_zero()
        res.settings.append({"master_gain": {"requested": 0, "read_back": gain}})
        if gain != 0:
            raise Gate2Error(f"master gain reads {gain}, requested 0")
        want_mode = IS_AUTO_BLACKLEVEL_OFF if mode_settable else bl["mode"]
        got = b.set_blacklevel_manual(offset, mode_settable=mode_settable)
        res.settings.append({"blacklevel": {"requested": {"mode": want_mode, "offset": offset},
                                            "mode_settable": mode_settable, "read_back": got}})
        if got != {"mode": want_mode, "offset": offset}:
            raise Gate2Error(f"black level reads {got}, requested mode {want_mode}, offset {offset}")
        for ms in EXPOSURES_MS:
            stage = f"dark frames {ms:g} ms"
            rb = b.set_exposure(ms)
            res.settings.append({"exposure_ms": {"requested": ms, "read_back": rb}})
            if abs(rb - ms) > EXPOSURE_TOL_MS:
                raise Gate2Error(f"exposure reads {rb:.4g} ms, requested {ms} ms")
            frames, bits, skipped = grab_series(b, FRAMES_DISCARDED, FRAMES_KEPT)
            if any(f.shape != SHAPE for f in frames):
                raise Gate2Error(f"frames are {frames[0].shape}, expected {SHAPE} raw")
            stats = dark_stats(frames, bits)
            stats["grabs_skipped"] = skipped
            res.exposures[f"{ms:g}ms"] = stats
            evidence_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            fpath = evidence_dir / f"{EVIDENCE_PREFIX}-{stamp}-{ms:g}ms.npy"
            np.save(fpath, np.stack(frames))
            res.frame_files.append(fpath.name)
            if stats["mean"] > 0.25 * ((1 << bits) - 1):
                raise Gate2Error(f"frames at {ms:g} ms are bright (mean {stats['mean']:.0f}): light leak")
        stage = "done"
    except Exception as exc:  # noqa: BLE001 - restore, report, never retry
        res.outcome = "stopped"
        res.message = f"{stage} failed: {exc}; not retried"

    # restore = exit (always); verify with a fresh init only if anything was changed
    try:
        if changed and res.snapshot:
            b.reopen()
            after = b.snapshot()
            res.restore_snapshot = after
            res.restore_diffs = compare_state(res.snapshot, after, COMPARED + ("blacklevel", "gain_boost"))
            res.restore_verified = not res.restore_diffs
            if not res.restore_verified:
                res.message += ("; RESTORE NOT VERIFIED: power-cycle the instrument, then re-run Camera Gate 1. "
                                f"Differences: {res.restore_diffs}")
        elif res.opened:
            res.restore_verified = True  # nothing was set
    except Exception as exc:  # noqa: BLE001
        res.restore_diffs = [f"restore check failed: {exc}"]
        res.message += f"; RESTORE NOT VERIFIED ({exc}): power-cycle the instrument, then re-run Camera Gate 1"
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
        res.message = f"dark frames clip at 0 at {clipped}; restore verified"
    elif any(s["saturated"] for s in res.exposures.values()):
        res.outcome = "stopped"
        res.message = "saturated pixels in a dark frame; restore verified"
    else:
        res.outcome, res.passed = "complete", True
        mode_name = next(t["name"] for t in res.color_modes_tried if t["accepted"])
        res.message = (f"{mode_name}: settings read back, unclipped dark frames, restore verified. "
                       + ", ".join(f"{k}: mean {s['mean']:.1f}, noise {s.get('temporal_noise', 0):.2f}"
                                   for k, s in res.exposures.items()))
    return res


def write_evidence(result: Gate2Result, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    path = out_dir / f"{EVIDENCE_PREFIX}-{stamp}-{result.outcome}.json"
    path.write_text(result.to_json(), encoding="utf-8")
    return path


def main(argv: list[str] | None = None, pnp: g1.PnpEnumerator | None = None,
         backend: UeyeSettingsBackend | None = None, busy=None) -> int:
    p = argparse.ArgumentParser(prog="python -m openevos.maintenance.camera_gate2_colour",
                                description="Camera Gate 2 (colour): RAM-only settings, raw Bayer dark frames, "
                                            "restore. Dry run by default.")
    p.add_argument("--send", action="store_true")
    p.add_argument("--confirm", default="", help=f"must be exactly: {CONFIRM_PHRASE}")
    p.add_argument("--evidence-dir", type=Path, default=Path.cwd() / "openevos-scans")
    a = p.parse_args(argv)
    if pnp is None:
        if sys.platform != "win32":
            print("This tool needs Windows.", file=sys.stderr)
            return 2
        pnp = g1.WindowsPnp()
    backend = backend or UeyeSettingsBackend()
    res = run_gate2(pnp, backend, evidence_dir=a.evidence_dir, send=a.send, confirm=a.confirm,
                    busy=busy or app_holds_cameras)
    print(f"[{res.outcome}] {res.message}")
    if res.opened or res.outcome == "dry_run":
        print(f"evidence: {write_evidence(res, a.evidence_dir)}")
    return 0 if res.outcome == "dry_run" or res.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
