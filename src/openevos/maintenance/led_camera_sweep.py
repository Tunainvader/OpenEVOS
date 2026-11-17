"""LED brightness sweep measured with the mono camera (ADR-0050): relative LED output vs level (M8, relative).

Spec: docs/evidence/fl1_v1.6_led_app_command_table.md §8.

Combines two reviewed gates, nothing new is sent to either device:
* LED Gate 5 (ADR-0049): `0x5E` field 0x04 / `0x4C` level 0–31 with read-back via `0x5D` / `0x4A`; `0x43` / `0x49`
  on/off; `0x47` cube check (epi refused for an empty slot or DAPI). Original level restored and read back.
* Camera Gate 2 mono (ADR-0036): Chameleon 1E10:2005, RAM-only Format7 1296x964 MONO16, auto exposure off, gain
  0 dB, brightness 2 %, shutter one of the reviewed values (1 / 10 / 50 ms). Power-up camera state restored and
  verified by snapshot comparison.

Sequence (one attempt; any failure -> light off -> LED level restored -> camera restored -> stop):
  LED: read original level (and the cube in the path for epi). Camera: connect, identity, snapshot (must match the
  Gate 1 record), apply the Gate 2 settings and read them back. Dark frame with the light OFF. Light ON; for each
  level: set + read back, discard 1 frame, keep 3, statistics. Stops early if a level saturates (no higher level
  is sent). Light OFF (confirmed), original level restored (read back), camera restored (verified), closed.
Output: per-level mean minus dark, monotonicity and a straight-line fit through the levels. Relative only: the
camera measures light at the sensor through the current objective / cube / sample, not optical power.
Dry run by default; needs the checklist and the confirmation phrase.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from openevos.backends.flycapture2.sdk import (
    BRIGHTNESS_PCT,
    F7_HEIGHT,
    F7_WIDTH,
    FlyCapture2SettingsBackend,
    compare_state,
    restore_state,
)
from openevos.maintenance import camera_gate1 as g1
from openevos.maintenance import led_brightness_test as bt
from openevos.maintenance.camera_gate2_mono import SHUTTERS_MS, find_gate1_record
from openevos.maintenance.led_bootloader import (
    PID_BOOTLOADER,
    REPORT_LEN,
    VID,
    HidBackend,
    WindowsHid,
)
from openevos.maintenance.led_checksum import find_app_start_record
from openevos.maintenance.led_status_read import PID_APPLICATION, PID_APPLICATION_ALT

GAIN_DB = 0.0
FRAMES_DISCARDED, FRAMES_KEPT = 1, 3
GRAB_TIMEOUT_S = 5.0
SETTLE_S = 0.2
MAX_LEVEL = {"brightfield": 20, "epi": 10}  # the app's slider caps (ADR-0048)
DEFAULT_LEVELS = (1, 2, 3, 5, 8, 10)
EPI_MAX_ON_S = 60.0
#: Full scale of the Chameleon's MONO16 output: 12-bit data shifted left by 4 (4095 << 4 = 65520). Found on the first
#: live sweep (2026-11-16), where levels 8 and 10 read exactly 65520 everywhere and were not flagged at 65535.
SATURATION = 65520
SATURATED_FRACTION_STOP = 0.001
CONFIRM_PHRASE = "LED CAMERA SWEEP ON 0DAD:0051 AND 1E10:2005 RAM ONLY"
CHECKLIST_PHRASE = bt.CHECKLIST_PHRASE
SPEC = "docs/evidence/fl1_v1.6_led_app_command_table.md §8; ADR-0050"
EVIDENCE_PREFIX = "led-camera-sweep"
ROLE = g1.ROLES["mono"]


class SweepStop(Exception):
    pass


def frame_stats(frames: list[np.ndarray]) -> dict[str, Any]:
    st = np.stack(frames).astype(np.float64)
    return {"mean": float(st.mean()), "p99_9": float(np.percentile(st[0], 99.9)), "max": float(st.max()),
            "saturated_fraction": float((st >= SATURATION).mean()),
            "temporal_noise": float((st[0] - st[1]).std() / np.sqrt(2)) if len(frames) > 1 else 0.0}


def analyse(dark_mean: float, rows: list[dict]) -> dict[str, Any]:
    pts = [(r["level"], r["mean"] - dark_mean) for r in rows]
    if not pts:
        return {}
    lv = np.array([p[0] for p in pts], float)
    net = np.array([p[1] for p in pts], float)
    out: dict[str, Any] = {"net_by_level": {int(a): round(float(b), 2) for a, b in pts},
                           "monotonic": bool(np.all(np.diff(net) > 0)) if len(net) > 1 else None}
    if len(net) >= 3:
        slope, icpt = np.polyfit(lv, net, 1)
        pred = slope * lv + icpt
        ss_res, ss_tot = float(((net - pred) ** 2).sum()), float(((net - net.mean()) ** 2).sum())
        out.update({"fit_slope_per_level": round(float(slope), 3), "fit_intercept": round(float(icpt), 2),
                    "fit_r2": round(1 - ss_res / ss_tot, 5) if ss_tot > 0 else None})
    return out


@dataclass
class SweepResult:
    outcome: str  # dry_run | refused | stopped | saturated | complete | off_failed | restore_failed
    message: str
    started_utc: str
    host: str
    light: str = ""
    levels: list[int] = field(default_factory=list)
    shutter_ms: float = 10.0
    original_level: int | None = None
    cube_in_path: str | None = None
    camera_identity: dict = field(default_factory=dict)
    camera_settings: list[dict] = field(default_factory=list)
    dark: dict = field(default_factory=dict)
    rows: list[dict] = field(default_factory=list)
    analysis: dict = field(default_factory=dict)
    camera_restore_diffs: list[str] | None = None
    camera_restore_verified: bool = False
    frame_files: list[str] = field(default_factory=list)
    led_device: Any = None
    app_start_record: str | None = None
    sent: bool = False
    steps: list[bt.Step] = field(default_factory=list)  # LED exchanges (shared with led_brightness_test)
    spec: str = SPEC

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, ensure_ascii=False, default=str)


def _check_prop(res: SweepResult, cam, name: str, want: dict[str, Any]) -> None:
    from openevos.backends.flycapture2.sdk import TOL

    got = cam.get_prop(name)
    res.camera_settings.append({"property": name, "requested": want, "read_back": got})
    for k in ("on", "auto"):
        if k in want and got[k] != want[k]:
            raise SweepStop(f"camera {name}.{k} read back {got[k]}, requested {want[k]}")
    if "abs_value" in want and abs(got["abs_value"] - want["abs_value"]) > TOL[name](want["abs_value"]):
        raise SweepStop(f"camera {name} read back {got['abs_value']:.4g}, requested {want['abs_value']}")


def _grab(cam, res: SweepResult, evidence_dir: Path, tag: str) -> dict[str, Any]:
    frames = cam.grab_series(FRAMES_DISCARDED, FRAMES_KEPT, GRAB_TIMEOUT_S)
    if any(f.shape != (F7_HEIGHT, F7_WIDTH) or f.dtype != np.uint16 for f in frames):
        raise SweepStop(f"frames are {frames[0].shape} {frames[0].dtype}, expected ({F7_HEIGHT}, {F7_WIDTH}) uint16")
    stats = frame_stats(frames)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    path = evidence_dir / f"{EVIDENCE_PREFIX}-{stamp}-{tag}.npy"
    np.save(path, frames[0])  # one frame per level as evidence (git-ignored folder)
    res.frame_files.append(path.name)
    return stats


def led_camera_sweep(led: HidBackend, pnp: g1.PnpEnumerator, cam: FlyCapture2SettingsBackend, *,
                     evidence_dir: Path, light: str, levels: tuple[int, ...] = DEFAULT_LEVELS,
                     shutter_ms: float = 10.0, send: bool = False, confirm: str = "", checklist: str = "",
                     monotonic: Callable[[], float] = time.monotonic,
                     sleep: Callable[[float], None] = time.sleep) -> SweepResult:
    res = SweepResult("refused", "", datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                      platform.node(), light, list(levels), shutter_ms)
    # ---------------------------------------------------------------- arguments
    if light not in MAX_LEVEL:
        res.message = "light must be 'brightfield' or 'epi'; nothing sent"
        return res
    if not levels or list(levels) != sorted(set(levels)) or levels[0] < 1 or levels[-1] > MAX_LEVEL[light]:
        res.message = f"levels must be strictly increasing within 1..{MAX_LEVEL[light]} for {light}; nothing sent"
        return res
    if shutter_ms not in SHUTTERS_MS:
        res.message = f"shutter must be one of the reviewed values {SHUTTERS_MS} ms; nothing sent"
        return res
    # ---------------------------------------------------------------- LED preconditions
    apps = led.find(VID, PID_APPLICATION)
    if len(apps) != 1 or led.find(VID, PID_BOOTLOADER) or led.find(VID, PID_APPLICATION_ALT):
        res.message = "need exactly one LED board application 0DAD:0051 and no 0057/0052; nothing sent"
        return res
    desc = led.describe(apps[0])
    res.led_device = desc
    if (desc.vendor_id, desc.product_id, desc.input_report_len, desc.output_report_len) != \
            (VID, PID_APPLICATION, REPORT_LEN, REPORT_LEN):
        res.message = "LED board does not report 0DAD:0051 with 65/65-byte reports; nothing sent"
        return res
    rec, problem = find_app_start_record(evidence_dir, desc.path)
    if rec is None:
        res.message = problem + "; nothing sent"
        return res
    res.app_start_record = rec.name
    # ---------------------------------------------------------------- camera preconditions
    devs = pnp.present(ROLE.vid, ROLE.pid)
    if len(devs) != 1 or devs[0].problem_code != 0:
        res.message = f"need exactly one {ROLE.vid_pid} camera with a working driver; nothing sent"
        return res
    if not cam.sdk_available() or cam.count_cameras() != 1:
        res.message = "FlyCapture2 SDK missing or does not see exactly one camera; nothing sent"
        return res
    if not list(evidence_dir.glob("camera-gate1-mono-*.json")):
        res.message = "no Camera Gate 1 record in the evidence folder; nothing sent"
        return res
    if not send:
        res.outcome = "dry_run"
        res.message = (f"dry run: would set the camera to MONO16 {shutter_ms:g} ms gain {GAIN_DB} dB, grab a dark "
                       f"frame, switch {light} on and step levels {list(levels)} (3 frames each, stop at saturation), "
                       "switch off, restore the LED level and the camera; nothing sent")
        return res
    if checklist != CHECKLIST_PHRASE:
        res.message = f"checklist phrase missing or wrong (expected {CHECKLIST_PHRASE!r}); nothing sent"
        return res
    if confirm != CONFIRM_PHRASE:
        res.message = f"confirmation phrase missing or wrong (expected {CONFIRM_PHRASE!r}); nothing sent"
        return res

    bf = light == "brightfield"
    get_cmd, key = (bt.CMD_PEARL_GET, "bf_level") if bf else (bt.CMD_EPI_STATUS, "epi_level")
    set_cmd = bt.CMD_PEARL_SET if bf else bt.CMD_EPI_LEVEL
    en_cmd = bt.CMD_BF_ENABLE if bf else bt.CMD_EPI_ENABLE
    maybe_on = level_changed = cam_changed = False
    snap: dict[str, Any] | None = None
    failed = ""
    saturated_at: int | None = None
    on_since: float | None = None
    res.sent = True
    with led.open(desc.path) as ch:

        def x(name, cmd, value=None):
            try:
                return bt._exchange(ch, res, name, cmd, value, monotonic)
            except bt._Failed as exc:
                raise SweepStop(str(exc)) from exc

        try:
            before = x(f"read {light} level", get_cmd)
            orig, rng = before[key], before["bf_range" if bf else "epi_range"]
            if not 0 <= orig <= 31 or rng != [0, 31]:
                raise SweepStop(f"unexpected {light} level {orig} / range {rng}")
            res.original_level = orig
            if not bf:
                res.cube_in_path = x("read cube in path", bt.CMD_POSITIONS)["cube_in_path"]
                if res.cube_in_path in bt.EPI_REFUSED_CUBES or res.cube_in_path.startswith("type "):
                    raise SweepStop(f"cube in the light path is {res.cube_in_path}; the sweep needs RFP or GFP")
            # camera: Gate 2 settings
            cam.connect()
            res.camera_identity = cam.identity()
            _rec_path, rec1 = find_gate1_record(evidence_dir, res.camera_identity["serial"])
            if rec1 is None:
                raise SweepStop("no completed Camera Gate 1 record for this camera serial")
            snap = cam.snapshot()
            diffs = compare_state(rec1["modes"], snap)
            if diffs:
                snap = None
                raise SweepStop("camera state differs from its Gate 1 record: " + "; ".join(diffs))
            cam_changed = True
            res.camera_settings.append({"format7": cam.set_format7_mono16_full()})
            cam.set_prop("auto_exposure", on=False, auto=False)
            _check_prop(res, cam, "auto_exposure", {"on": False, "auto": False})
            cam.set_prop("gain", on=True, auto=False, abs_value=GAIN_DB)
            _check_prop(res, cam, "gain", {"auto": False, "abs_value": GAIN_DB})
            cam.set_prop("brightness", on=True, auto=False, abs_value=BRIGHTNESS_PCT)
            _check_prop(res, cam, "brightness", {"abs_value": BRIGHTNESS_PCT})
            cam.set_prop("shutter", on=True, auto=False, abs_value=shutter_ms)
            _check_prop(res, cam, "shutter", {"auto": False, "abs_value": shutter_ms})
            res.dark = _grab(cam, res, evidence_dir, "dark")
            # light on, step the levels
            for lvl in levels:
                level_changed = True
                x(f"set {light} level {lvl}", set_cmd, lvl)
                if x(f"read {light} level", get_cmd)[key] != lvl:
                    raise SweepStop(f"{light} level did not read back {lvl}")
                if not maybe_on:
                    maybe_on = True
                    if x(f"{light} ON", en_cmd, 1)["readback"] != 1:
                        raise SweepStop(f"{light} ON did not read back 1")
                    on_since = monotonic()
                sleep(SETTLE_S)
                stats = _grab(cam, res, evidence_dir, f"{light}-L{lvl}")
                stats["level"] = lvl
                res.rows.append(stats)
                if not bf and on_since is not None and monotonic() - on_since > EPI_MAX_ON_S:
                    raise SweepStop(f"epi on longer than {EPI_MAX_ON_S:g} s; stopped")
                if stats["saturated_fraction"] > SATURATED_FRACTION_STOP:
                    saturated_at = lvl
                    break
            if x(f"{light} OFF", en_cmd, 0)["readback"] != 0:
                raise SweepStop(f"{light} OFF did not read back 0")
            maybe_on = False
            x(f"restore {light} level {orig}", set_cmd, orig)
            if x(f"read {light} level (after restore)", get_cmd)[key] != orig:
                raise SweepStop("restored level did not read back")
            level_changed = False
        except Exception as exc:  # noqa: BLE001 - stop safely, never retry
            failed = str(exc) or type(exc).__name__
        if maybe_on:
            try:
                if x(f"{light} OFF (after failure)", en_cmd, 0)["readback"] == 0:
                    maybe_on = False
            except SweepStop:
                pass
        if level_changed and res.original_level is not None:
            try:
                x(f"restore {light} level (after failure)", set_cmd, res.original_level)
                if x(f"read {light} level (after failure restore)", get_cmd)[key] == res.original_level:
                    level_changed = False
            except SweepStop:
                pass
    # camera restore (always, if anything was changed)
    if cam_changed and snap is not None:
        try:
            restore_state(cam, snap)
            res.camera_restore_diffs = compare_state(snap, cam.snapshot())
            res.camera_restore_verified = not res.camera_restore_diffs
        except Exception as exc:  # noqa: BLE001
            res.camera_restore_diffs = [f"restore failed: {exc}"]
    try:
        cam.close()
    except Exception:  # noqa: BLE001, S110 - best effort
        pass
    res.analysis = analyse(res.dark.get("mean", 0.0), res.rows)

    if maybe_on:
        res.outcome = "off_failed"
        res.message = f"{failed}; LED OFF not confirmed. Power-cycle the instrument now"
    elif level_changed:
        res.outcome = "restore_failed"
        res.message = f"{failed}; light off, but the original LED level was not confirmed restored. Power-cycle"
    elif cam_changed and not res.camera_restore_verified:
        res.outcome = "restore_failed"
        res.message = (f"{failed + '; ' if failed else ''}camera restore NOT verified {res.camera_restore_diffs}: "
                       "power-cycle the instrument, then re-run Camera Gate 1")
    elif failed:
        res.outcome = "stopped"
        res.message = f"{failed}; light off, LED level and camera restored; not retried"
    elif saturated_at is not None:
        res.outcome = "saturated"
        res.message = (f"sweep stopped at level {saturated_at} (saturation); light off, level and camera restored. "
                       f"Net means: {res.analysis.get('net_by_level')}")
    else:
        res.outcome = "complete"
        a = res.analysis
        res.message = (f"{light} sweep complete; light off, level and camera restored. Net mean by level: "
                       f"{a.get('net_by_level')}; monotonic {a.get('monotonic')}; linear R² {a.get('fit_r2')}")
    return res


def write_evidence(result: SweepResult, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    path = out_dir / f"{EVIDENCE_PREFIX}-{stamp}-{result.outcome}.json"
    path.write_text(result.to_json(), encoding="utf-8")
    return path


def main(argv: list[str] | None = None, led: HidBackend | None = None, pnp: g1.PnpEnumerator | None = None,
         cam: FlyCapture2SettingsBackend | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m openevos.maintenance.led_camera_sweep",
                                description="Measure relative LED output vs level with the mono camera. Dry run by default.")
    p.add_argument("--light", choices=sorted(MAX_LEVEL), required=True)
    p.add_argument("--levels", default=",".join(map(str, DEFAULT_LEVELS)), help="comma-separated, increasing")
    p.add_argument("--shutter-ms", type=float, default=10.0, help=f"one of {SHUTTERS_MS}")
    p.add_argument("--send", action="store_true")
    p.add_argument("--checklist", default="", help=f"must be exactly: {CHECKLIST_PHRASE}")
    p.add_argument("--confirm", default="", help=f"must be exactly: {CONFIRM_PHRASE}")
    p.add_argument("--evidence-dir", type=Path, default=Path.cwd() / "openevos-scans")
    a = p.parse_args(argv)
    try:
        levels = tuple(int(v) for v in a.levels.split(",") if v.strip())
    except ValueError:
        p.error("--levels must be integers")
    if led is None or pnp is None:
        if sys.platform != "win32":
            print("This tool needs Windows.", file=sys.stderr)
            return 2
        led = led or WindowsHid()
        pnp = pnp or g1.WindowsPnp()
    cam = cam or FlyCapture2SettingsBackend()
    res = led_camera_sweep(led, pnp, cam, evidence_dir=a.evidence_dir, light=a.light, levels=levels,
                           shutter_ms=a.shutter_ms, send=a.send, confirm=a.confirm, checklist=a.checklist)
    print(f"[{res.outcome}] {res.message}")
    if res.dark:
        print(f"  dark: mean {res.dark['mean']:.1f}")
    for r in res.rows:
        print(f"  level {r['level']:2d}: mean {r['mean']:.1f}  p99.9 {r['p99_9']:.0f}  saturated "
              f"{r['saturated_fraction'] * 100:.3f} %")
    if res.sent or res.outcome == "dry_run":
        print(f"evidence: {write_evidence(res, a.evidence_dir)}")
    return 0 if res.outcome in ("dry_run", "complete", "saturated") else 1


if __name__ == "__main__":
    raise SystemExit(main())
