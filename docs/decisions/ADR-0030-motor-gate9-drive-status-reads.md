# ADR-0030: Motor Gate 9, read-only drive status/error registers, and safer move completion

- **Status:** Accepted, implemented (2026-11-13). Tested with fake drive chains; first run on the instrument pending approval.
- **Amends:** ADR-0019 (Gate 4 move loop, shared by Gates 5, 6 and 8) and ADR-0028 (Gate 8 evidence).
- **Spec:** `docs/evidence/fl1_v1.6_motor_gate9_drive_status_spec.md`.
- **Evidence:**
  - `motor-gate8-20261113T111251-fault_stopped` (Y home leg ended short; stale completion state at the first poll);
  - `motor-gate2-20261113T113143-complete` (Y idle at 166,659 after the return-to-zero attempt);
  - `motor-gate2-20261113T114015-complete` (after the power cycle: X/Y homed at 0, Z `LPOSMIN` −1783).

## Context
Y twice ended a move toward 0 on its own near 73.5 mm (CPOS = APOS short of the target). The reviewed reads
could not say why. The move loop also accepted a stale "complete" state, and its timeout counted polls, so a
15 s timeout became ~6 minutes on the real line.

## Decision
- **Gate 9:** a read-only tool (opcode 0xB004 only, empty write allowlist) reads `MER`, `SRL`, `SRH`, `MSR`,
  `ISR`, `INSTATUS`, `POSERR`, `ERRMAX`, `TERRMAX` on X, Y and Z. Addresses Verified from each `variables.cfg`;
  bit names Probable.
- **Gate 8** also reads the X/Y status frames: before motion, after any fault stop (never before it), and at the end.
- **Move loop (`_leg`):**
  - completion requires `CPOS` = target as well as SRH bit 9 + ESTATEV 13;
  - a move the drive abandons (not moving, CPOS frozen short of target for 3 polls) faults at once;
  - the timeout is bounded by wall-clock time.
- Registered in `REVIEWED_L3_READS["motor.gate9_status_read"]`.

## Consequences
- The next unexplained stop will record the drive's own error and limit-switch state.
- No new writes anywhere; Gate 8's write allowlist is unchanged.
- Each extra read costs ~0.4 s on the line, so a Gate 8 run is a few seconds longer.

## Amendment (2026-11-13): root cause and commutation-drift interlock
- **Over-current confirmed:** Y peak 17,198 > `IMAXPROT` 17,117 on a wrong-direction runaway.
- **Trigger:** the runaways follow X moves. An X-only test showed idle Y's encoder creeping to −9 counts.
- **Mechanism (strongly supported):** commutation offset on the closed-loop stepper. 1 count = 6.25° electrical; ~14 counts reverses torque.
- **Decision:**
  - |`APOS` − `CPOS`| ≤ 3 counts is a precondition for every X/Y move (tools and app);
  - drift > 3 counts on an idle axis during another axis' move is a `cross_axis_encoder_drift` fault and locks that axis until a power-cycle re-home.
- **Hardware follow-up:** inspect the Y encoder cable routing, shield and connector. Acceptance: in the X-only watch test, Y stays at 0 ± 1.
