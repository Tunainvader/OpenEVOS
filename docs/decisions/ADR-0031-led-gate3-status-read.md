# ADR-0031: LED application command table (RE-4) and LED Gate 3, a read-only status query

- **Status:** Accepted. Run once on the instrument, 2026-11-13 15:45 UTC, after Gate 1 and StartApp, operator-approved: all four replies valid, application 3.0.25949, cube types at positions 1–4 = 5, 1, 3, 4, i.e. RFP, empty, DAPI, GFP via the type-library enum `Gen3FilterCubeType` (CANDIDATE until checked physically, spec §4b). Replies contain residue of earlier replies, so only bytes the firmware handler writes are meaningful (spec §4a).
- **Spec:** [`fl1_v1.6_led_app_command_table.md`](../evidence/fl1_v1.6_led_app_command_table.md)

## Context

- Plan item RE-4 asks for the LED board application's commands (channel select, intensity, enable, cube identity, LED temperature/current) before gate M8 (LED linearity and stability).
- The LED board already runs its application (`0DAD:0051`, 3.0.25949) after the reviewed StartApp (ADR-0012), and Gate 2 proved the framing (ADR-0013 to ADR-0015).

## Decision

1. **Command table.** All 152 `IGen3LedBoard` methods were mapped to HID command bytes from the vendor DLL (Ghidra, read-only). Every byte is classified read-only / RAM write / persistent write / firmware. Persistent (`0x52`, `0x32`, and `0x5E` until its sub-selectors are decoded) and firmware (`0x33`–`0x36`) commands stay banned under ADR-0009. RAM writes (`0x41`–`0x4F`, `0x5B`, `0x54`, `0x46`) need their own reviewed gate.
2. **No LED temperature or measured-current query exists** in the vendor interface. M8 will measure light optically (power meter or camera), not from board telemetry.
3. **LED Gate 3** = `python -m openevos.maintenance.led_status_read`: four bare read requests `0x40`, `0x47`, `0x5A`, `0x4A`, once each in that order, stopping at the first failure. It gives application version, cube / objective / phase-ring positions, cube identity at positions 1–4, epi mode and channel states, epi intensity (digipot), current mode and PWM. Registered as `REVIEWED_L3_READS["led_application.gate3_status_read"]`.
4. Same safety envelope as Step B: dry run by default; exact phrase `READ LED STATUS 40 47 5A 4A ON 0DAD:0051`; one write per command, 500 ms limits, at most 5 passive reads per command, never resend, BUSY stops; JSON evidence in git-ignored `openevos-scans/`. Decoded values are CANDIDATE.

## Consequences

- Cube identity can be compared with the physically installed cubes without any write.
- The firmware handlers for these commands are not traced (our build is not in the package). The host-side evidence is strong (the vendor polls `0x47` continuously), but the first run is still an L3 step with operator approval.
- Next (separate review): LED Gate 4, a minimal RAM-write test of channel select + lowest intensity + enable/disable, with light-path safety rules.
