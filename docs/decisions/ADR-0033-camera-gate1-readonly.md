# ADR-0033: Camera Gate 1, read-only identity, modes and one dark frame

- **Status:** Accepted. Mono run once on 2026-11-13 18:04 UTC, operator-approved, LEDs off. Every step completed: Chameleon CMLN-13S2M, firmware 1.17.3.0, power-up mode 1280×960 Y8 at 15 fps with auto shutter and gain, and a dark frame (mean 0.014 / 255). It was flagged "stopped" only by an over-strict shape check, which has since been fixed (spec §4b). The colour role waits for the IDS SDK.
- **Spec:** [`camera_gate1_spec.md`](../evidence/camera_gate1_spec.md); enumeration: [`camera_usb_enumeration_2026-11-13.md`](../evidence/camera_usb_enumeration_2026-11-13.md).

## Context

- Both cameras are present on the microscope hub (mono `1E10:2005`, colour `1409:1000`), both vendor-class with no driver. A vendor SDK is needed before any image (FlyCapture2 for the Chameleon, IDS Software Suite for the uEye).
- Cameras hold persistent state (user sets, EEPROM) and SDKs can update firmware, so the first contact must be read-only, as with the motor and LED gates (ADR-0009).

## Decision

1. **Camera Gate 1** = `python -m openevos.maintenance.camera_gate1 --role mono|colour`: preconditions, connect, identity, mode list, **one** frame at power-up settings with the LED off, close. One role per run.
2. **Read-only by construction.** The gate talks to a `CameraGateBackend` with exactly `connect / identity / modes / grab_one / close`. Persistent, firmware, register and reset calls have no method. Real adapters (FlyCapture2 C, uEye) are added only after the user approves the SDK installs, and are reviewed against this list.
3. **Safety envelope as the LED gates:** dry run by default; exact phrase `READ CAMERA <ROLE> <VID:PID> WITH LED OFF`; one attempt, no retry; close always called; JSON + `.npy` evidence in git-ignored `openevos-scans/`. Every run needs the user's approval.
4. **No downloads or installs without the user's approval** (file, source, size stated). SDKs are installed without firmware updaters.

## Consequences

- M2 camera part is answered except the uEye serial. M9/M10 step 1 is this gate.
- Next, after a passing run: a `CameraDevice` backend for that role in `backends/` (same SDK adapter), wired into the Capture page in place of the simulated frames for that role only, with the SIMULATED badge off for real frames.
