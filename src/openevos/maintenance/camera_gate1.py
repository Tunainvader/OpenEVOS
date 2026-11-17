"""Camera Gate 1: read-only identity, modes and one dark frame (ADR-0033, spec camera_gate1_spec.md).

One camera role per run (mono `1E10:2005` Chameleon via FlyCapture2, colour `1409:1465` uEye UI-146xLE via
the classic uEye API, `backends/ueye/sdk.py`). It:
* checks that exactly one device of that VID:PID is present with a driver bound (PnP problem code 0)
  and that the vendor SDK library is present;
* dry run by default: prints the call plan and opens nothing;
* with `--send` and the exact phrase: connect, identity, modes, ONE frame at the camera's current
  settings (LED off), close. One attempt, no retry; close is always called.

The backend interface has only connect / identity / modes / grab_one / close. Persistent writes,
firmware, register writes and resets cannot be expressed. Adapters: `backends/flycapture2/sdk.py` (mono) and
`backends/ueye/sdk.py` (colour), each with an allow-list of read-only calls.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

import numpy as np

SPEC = "docs/evidence/camera_gate1_spec.md; ADR-0033"
EVIDENCE_PREFIX = "camera-gate1"
GRAB_TIMEOUT_S = 5.0
DARK_MEAN_LIMIT = 0.25  # fraction of full range


@dataclass(frozen=True)
class Role:
    name: str
    vid: int
    pid: int
    sdk_library: str
    expected_shapes: tuple[tuple[int, int], ...] | None  # allowed (H, W); None = take the SDK's word

    @property
    def vid_pid(self) -> str:
        return f"{self.vid:04X}:{self.pid:04X}"

    @property
    def phrase(self) -> str:
        return f"READ CAMERA {self.name.upper()} {self.vid_pid} WITH LED OFF"


ROLES = {
    "mono": Role("mono", 0x1E10, 0x2005, "FlyCapture2_C_v140.dll", ((964, 1296), (960, 1280))),  # Format7 full / IIDC 1280x960
    # uEye UI-146xLE: 1409:1000 is its "uEye boot" state; with the IDS driver it runs as 1409:1465
    "colour": Role("colour", 0x1409, 0x1465, "ueye_api_64.dll", None),
}


class GateRefused(RuntimeError):
    """A precondition failed; nothing was opened."""


@dataclass(frozen=True)
class PnpDevice:
    instance_id: str
    vid: int
    pid: int
    problem_code: int


class CameraGateBackend(Protocol):
    """The only operations the gate can perform. Deliberately no setters."""

    def sdk_available(self) -> bool: ...
    def count_cameras(self) -> int: ...
    def connect(self) -> None: ...
    def identity(self) -> dict[str, Any]: ...
    def modes(self) -> dict[str, Any]: ...
    def grab_one(self, timeout_s: float) -> tuple[np.ndarray, int]:
        """One frame at current settings: (pixels, bit_depth)."""
    def close(self) -> None: ...


class PnpEnumerator(Protocol):
    def present(self, vid: int, pid: int) -> list[PnpDevice]: ...


class WindowsPnp:
    """Read-only PnP query via PowerShell (`Get-PnpDevice`); opens no device."""

    def present(self, vid: int, pid: int) -> list[PnpDevice]:
        import subprocess

        pat = f"USB\\VID_{vid:04X}&PID_{pid:04X}\\*"
        cmd = ("Get-PnpDevice -PresentOnly -InstanceId '" + pat + "' -ErrorAction SilentlyContinue | "
               "% { $c=(Get-PnpDeviceProperty -InstanceId $_.InstanceId -KeyName DEVPKEY_Device_ProblemCode).Data;"
               " \"$($_.InstanceId)|$c\" }")
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
                             check=False, capture_output=True, text=True, timeout=60).stdout
        devs = []
        for line in out.splitlines():
            if "|" in line:
                iid, code = line.rsplit("|", 1)
                devs.append(PnpDevice(iid.strip(), vid, pid, int(code) if code.strip() else -1))
        return devs


class SdkNotIntegrated:
    """Placeholder until the SDK adapters are written and reviewed (spec §7)."""

    def __init__(self, role: Role):
        self.role = role

    def sdk_available(self) -> bool:
        return False

    def _no(self, *_a, **_k):
        raise GateRefused(f"{self.role.sdk_library} adapter not integrated yet (needs approved SDK install)")

    count_cameras = connect = identity = modes = grab_one = close = _no


@dataclass
class GateResult:
    role: str
    outcome: str  # "dry_run" | "refused" | "stopped" | "complete"
    message: str
    started_utc: str
    host: str
    pnp: list[dict] = field(default_factory=list)
    plan: list[str] = field(default_factory=lambda: ["connect", "identity", "modes", "grab_one", "close"])
    opened: bool = False
    steps_done: list[str] = field(default_factory=list)
    identity: dict = field(default_factory=dict)
    modes: dict = field(default_factory=dict)
    frame_stats: dict = field(default_factory=dict)
    frame_file: str | None = None
    passed: bool = False
    spec: str = SPEC

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, ensure_ascii=False, default=str)


def frame_stats(pixels: np.ndarray, bit_depth: int) -> dict[str, Any]:
    full = (1 << bit_depth) - 1
    p = pixels.astype(np.float64)
    lo, med, hi = np.percentile(p, [0.1, 50, 99.9])
    return {"shape": list(pixels.shape), "dtype": str(pixels.dtype), "bit_depth": bit_depth,
            "min": float(p.min()), "max": float(p.max()), "mean": float(p.mean()), "std": float(p.std()),
            "p0_1": float(lo), "p50": float(med), "p99_9": float(hi),
            "saturated": int((pixels >= full).sum()), "mean_fraction": float(p.mean() / full)}


def run_gate(role: Role, pnp: PnpEnumerator, backend: CameraGateBackend, *, send: bool = False,
             confirm: str = "", evidence_dir: Path | None = None) -> GateResult:
    res = GateResult(role.name, "refused", "", datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                     platform.node())
    devs = pnp.present(role.vid, role.pid)
    res.pnp = [asdict(d) for d in devs]
    if len(devs) != 1:
        res.message = f"expected exactly one present {role.vid_pid}, found {len(devs)}; nothing opened"
        return res
    if devs[0].problem_code != 0:
        res.message = (f"{role.vid_pid} has no working driver (problem code {devs[0].problem_code}); "
                       "nothing opened")
        return res
    if not backend.sdk_available():
        res.message = f"{role.sdk_library} not available; nothing opened"
        return res
    n = backend.count_cameras()
    if n != 1:
        res.message = f"SDK sees {n} cameras, expected 1; nothing opened"
        return res
    if not send:
        res.outcome = "dry_run"
        res.message = "dry run: would call " + ", ".join(res.plan) + " once each; nothing opened"
        return res
    if confirm != role.phrase:
        res.message = f"confirmation phrase missing or wrong (expected {role.phrase!r}); nothing opened"
        return res

    step = "connect"
    try:
        res.opened = True
        backend.connect()
        res.steps_done.append(step)
        step = "identity"
        res.identity = dict(backend.identity())
        res.steps_done.append(step)
        step = "modes"
        res.modes = dict(backend.modes())
        res.steps_done.append(step)
        step = "grab_one"
        pixels, depth = backend.grab_one(GRAB_TIMEOUT_S)
        res.steps_done.append(step)
    except Exception as exc:  # noqa: BLE001 - report, never retry
        res.outcome = "stopped"
        res.message = f"{step} failed: {exc}; not retried"
        _close(backend, res)
        return res
    _close(backend, res)

    res.frame_stats = frame_stats(pixels, depth)
    if evidence_dir is not None:
        evidence_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        f = evidence_dir / f"{EVIDENCE_PREFIX}-{role.name}-{stamp}-frame.npy"
        np.save(f, pixels)
        res.frame_file = f.name
    hw = tuple(pixels.shape[:2])
    s = res.frame_stats
    if role.expected_shapes and hw not in role.expected_shapes:
        res.outcome, res.message = "stopped", f"frame is {hw}, expected one of {role.expected_shapes}"
    elif s["saturated"] or s["mean_fraction"] > DARK_MEAN_LIMIT:
        res.outcome = "complete"
        res.message = (f"frame read but not dark (mean {s['mean_fraction']:.1%} of range, "
                       f"{s['saturated']} saturated): LED or room light not off; not a pass")
    else:
        res.outcome, res.passed = "complete", True
        res.message = f"identity, modes and one dark frame read (mean {s['mean']:.1f}, std {s['std']:.2f})"
    return res


def _close(backend: CameraGateBackend, res: GateResult) -> None:
    try:
        backend.close()
        res.steps_done.append("close")
    except Exception as exc:  # noqa: BLE001
        res.message += f"; close failed: {exc}"


def write_evidence(result: GateResult, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    path = out_dir / f"{EVIDENCE_PREFIX}-{result.role}-{stamp}-{result.outcome}.json"
    path.write_text(result.to_json(), encoding="utf-8")
    return path


def main(argv: list[str] | None = None, pnp: PnpEnumerator | None = None,
         backend: CameraGateBackend | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m openevos.maintenance.camera_gate1",
                                description="Camera Gate 1: read identity, modes and one dark frame "
                                            "(read-only). Dry run by default.")
    p.add_argument("--role", choices=sorted(ROLES), required=True)
    p.add_argument("--send", action="store_true", help="actually open the camera and grab one frame")
    p.add_argument("--confirm", default="", help="READ CAMERA <ROLE> <VID:PID> WITH LED OFF")
    p.add_argument("--evidence-dir", type=Path, default=Path.cwd() / "openevos-scans")
    a = p.parse_args(argv)
    role = ROLES[a.role]
    if pnp is None:
        if sys.platform != "win32":
            print("This tool needs Windows.", file=sys.stderr)
            return 2
        pnp = WindowsPnp()
    if backend is None:
        if role.name == "mono":
            from openevos.maintenance.flycapture2_gate import FlyCapture2GateBackend

            backend = FlyCapture2GateBackend()
        else:
            from openevos.backends.ueye.sdk import UeyeGateBackend

            backend = UeyeGateBackend()
    res = run_gate(role, pnp, backend, send=a.send, confirm=a.confirm, evidence_dir=a.evidence_dir)
    print(f"[{res.outcome}] {res.message}")
    if res.identity:
        print(f"identity: {res.identity}")
    if res.opened or res.outcome == "dry_run":
        print(f"evidence: {write_evidence(res, a.evidence_dir)}")
    return 0 if res.outcome == "dry_run" or res.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
