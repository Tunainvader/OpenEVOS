# ADR-0023: Read-only motor telemetry in the desktop app

- **Status:** Accepted (2026-11-13). Implemented and tested against a simulated drive chain. **Not yet run against the instrument.**
- **Builds on:** ADR-0007 to ADR-0011 (core, firmware safety, UI on the command core, maintenance isolation); Motor Gates 1–6 (ADR-0016 to ADR-0021), which verified every frame used here on the hardware.

## Context
The Motor Gates proved the read protocol, the per-axis addresses, the 32-bit word order and the motion-complete bit (`SRH` bit 9) on the real drives. The desktop app should show live positions and drive states without gaining any ability to move anything.

Constraints:
- The app's `core`, `hal`, `ui` and `backends/simulation` packages may not import serial/USB/FFI modules.
- Nothing in the app may import `openevos.maintenance`.
- `HalDeviceBackend` is simulation-only; a physical device must be a native `DeviceBackend` that reports its firmware mode.

## Decision
- **Package:** a new `openevos.backends.technosoft` package, outside the transport-banned packages; the firmware-name guard still applies:
  - `protocol.py`: a pure, read-only codec. Only `B000`/`B004`/`B005` frames can be built, and `assert_allowed` admits only the sync byte, the ID query and a fixed set of variable reads. Tests pin it against the Motor Gate frames.
  - `transport.py`: `WinSerialPort` (Gate 1 rev 2's vendor-identical port setup; it refuses an abnormal DTR/RTS state, and `write()` passes only allowlisted reads) and `SimulatedDriveChain`.
  - `telemetry.py`: one `TelemetryService` thread owns the port. It polls state, homed marker, `SRH`, `APOS`, `CPOS` and limits on all six axes, about once a second.
- **Devices:** five native **PHYSICAL** backends with **no supported commands**:
  - `motor.stage` (X+Y, µm), `motor.focus` (Z, µm);
  - `motor.cube_carriage`, `motor.objective_turret`, `motor.phase_wheel` (counts).

  `start()` fails every command with `read_only`, and `halt()` does nothing. Each reports connection, health, activity (`MOVING` while `SRH` bit 9 is clear), `firmware_mode` = APPLICATION, position, and scalar attributes (`homed`, `limits_mm`, raw counts, drive state).
- **Reads:** each read requests the exact reply length (13 or 15 bytes), so a read takes milliseconds rather than the gates' 0.4 s timeout. Polling is periodic: a failed cycle marks the devices WARNING and retries next cycle, and a port that cannot be opened is retried every 5 s.
- **Wiring:**
  - `backends.TELEMETRY` holds the names;
  - `InstrumentConfig.telemetry` says which to load;
  - `core.instrument.attach_telemetry` registers the devices and `Instrument.close()` stops the service;
  - the app flag is `--motor-telemetry COM3 | auto | sim`;
  - the Devices page shows a Position column, with the attributes as a tooltip.
- **Session:** the optics remain simulated. The top bar's "simulated" marker follows the HAL devices; the motor rows are labelled "(read-only)".

## Consequences
- **The port is exclusive:** while the app holds it, PearlScope and the Motor Gate tools cannot open it, and the reverse.
- **No path from the UI or the command core can send a write to the drives:** there are no commands, no write builder, and a transport-level allowlist.
- **Duplication:** the read codec duplicates a small part of the maintenance tools (deliberately, ADR-0011). Tests cross-check the two.
- **Next:** a reviewed Stop path and bounded jogs would be separate ADRs, built on Gates 3–6.
