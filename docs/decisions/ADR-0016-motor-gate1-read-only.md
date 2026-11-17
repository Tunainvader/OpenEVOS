# ADR-0016: Motor Gate 1, read-only first contact with the Technosoft drives

- **Status:** Accepted: implemented and tested against a fake serial drive chain (2026-11-12). **Nothing has been sent to the drives.** Running it on the instrument is a separate operator approval.
- **Builds on:**
  - ADR-0009 (firmware safety), ADR-0011 (Gate 1 pattern);
  - [`fl1_v1.6_motor_subsystem_l0.md`](../evidence/fl1_v1.6_motor_subsystem_l0.md) §3a;
  - the reviewed spec [`fl1_v1.6_motor_gate1_spec.md`](../evidence/fl1_v1.6_motor_gate1_spec.md).

## Decision

`python -m openevos.maintenance.motor_gate1` implements the spec exactly, and nothing more.

### What can be sent
- The single line-sync byte `FF` (at most 5 attempts).
- The gateway axis-ID query `08 00 00 B0 00 00 01 00 00 B9`.
- The 22 fixed variable reads of spec §5.1 (`ESTATEV`, `IINITONCEDONE`, `IVERSION`, `CPOS` per axis), computed from a table and pinned by a test against the spec's own table and against the vendor's probes.
- `build_read` accepts only opcodes `0xB004`/`0xB005`; `assert_allowed` runs before every write and passes only these constant frames. The baud change (`0x0820`), the axis-ID write (`0x0801`), TML text, writes, motion, enable, reset, EEPROM and `TS_*` calls cannot be produced.

### Port
- Identified read-only with `serial.tools.list_ports` (pyserial never opens it): exactly one FTDI `0403:6001` port, recorded with its hwid.
- Opened by `maintenance/win_serial.py`, which copies the vendor's `FUN_180041560`: exclusive `CreateFileW`, a **zero-filled DCB** at 9,600 8N2 (all flags 0: no DTR/RTS or flow control), read timeouts ≤ 200 ms, RX purge before every request. The baud rate is never changed.

### Preconditions (else nothing is opened)
- `--instrument-on-2min`: the operator confirms the EVOS has been on ≥ 2 minutes, so power-up homing has finished.
- The exact phrase `READ MOTOR DRIVES ON COMn`.
- Dry run by default.

### Stop rules
- Each request is sent once. Any timeout, bad acknowledgement, length, checksum, sender or variable mismatch stops the run, and the port is closed.
- Gateway ID ≠ 1: stop.
- `ESTATEV` ≠ 0, or `IINITONCEDONE` ≠ 12345 on X/Y/Z/F: stop with `not_idle`; no further axes are read.

### Output
Git-ignored JSON in `openevos-scans/`:
- the port identity;
- every request and reply in hex, with timings;
- 16-bit values, and 32-bit values in both word orders;
- `IVERSION` compared with `MotorFirmware.ini`;
- `CPOS` converted to candidate mm.

All values are **CANDIDATE**.

### Guards
- `REVIEWED_L3_READS["motor.gate1_read"]`.
- The firmware-name scan and the import guard apply. The maintenance package stays unreachable from the desktop app, and the command core still refuses every motor command.

## First run (2026-11-12, 18:15 UTC)

- **Result:** stopped at line sync. 5 × `FF` were sent at 9,600 with the zero-filled DCB, and no byte came back. Nothing else was sent.
- **Cause:** the "zero-filled DCB" premise above is **wrong**. The vendor calls `GetCommState` after zeroing, keeping DTR/RTS, and tries 115,200 first. Details are in spec §11.
- **Next:** a revised transport needs its own approval.

## Revision 2 (2026-11-12): accepted and implemented; supersedes "Port" above

- **`win_serial.py` copies `FUN_180041560`:**
  - opening the port changes nothing;
  - `inspect()` only reads (`GetCommState`/`GetCommTimeouts`);
  - `configure(baud)` takes the read-back DCB, changes only BaudRate/ByteSize/Parity/StopBits, accepts only 115,200 or 9,600, and sets the vendor's read timeouts (1000/10/10).
- **`motor_gate1.py`:**
  - `--inspect-port` (phrase `INSPECT MOTOR PORT COMn`) sends nothing;
  - `--send` refuses unless the read-back DCB has DTR/RTS control = ENABLE and no flow control;
  - it syncs at 115,200, then at 9,600;
  - before every frame it syncs (≤ 15 × `FF`, 10 ms apart), then writes the frame once, waits 50 ms and reads up to 32 bytes;
  - it never sends a baud or axis-ID command.
- **Inspect run (18:24 UTC):** the driver still held rev 1's DTR/RTS = 0 (spec §12). The full run was held until a later inspect showed the driver's normal state.
- **After the operator power-cycled the instrument,** the second inspect was normal (§12a).
- **Full run: complete** (§13).
  - All six drives answered at **115,200** on the first sync.
  - The gateway ID is 1; all drives are idle; X/Y/Z/F report `IINITONCEDONE` = 12345.
  - Every `IVERSION` equals `MotorFirmware.ini`, and `CPOS` = 0 everywhere.
  - Nothing was written to the drives beyond the reviewed read frames.
  - Rev 1's silence is explained by its 9,600-only attempt.

## Not covered
- The baud change, the axis-ID write, and any write, motion, enable, homing, reset or firmware/EEPROM operation.
- A motor backend for the command core: future work with its own reviews.
