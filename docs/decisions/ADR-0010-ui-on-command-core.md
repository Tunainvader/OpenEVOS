# ADR-0010: The desktop UI runs on the command core

- **Status:** Accepted (2026-11-12)
- **Builds on:**
  - ADR-0006: STOP keeps the incubator running.
  - ADR-0007: runtime state axes.
  - ADR-0008: core execution model.
  - ADR-0009: firmware safety.

## Context

Two execution paths existed. The Phase 2.1 core (`core.router`) and the desktop UI's path (`hal.gateway.InstrumentGateway` + `hal.command_policy`) both validated and applied commands. The core had five typed commands; the UI used nineteen gateway operations. Two validators can disagree, and only the core had:
- atomic reservations;
- three-axis state;
- capability provenance;
- the firmware quarantine.

## Decision

Every state change the desktop makes goes through `core.router`:

```
UI / MicroscopeController / AcquisitionEngine
  └─ CoreGateway.execute("name", **params)     parses parameters only (names, types, units)
       └─ typed Command (core.models)
            └─ CommandRouter: STOP latch → SafetyManager → atomic reservation → DeviceWorker
                 └─ HalDeviceBackend (simulated HAL device) → readback-verified completion
```

### Typed commands

Nine typed commands were added, so every gateway operation has a core equivalent:
- `stage.home` and `focus.home`;
- `illumination.select_carriage` and `illumination.all_off`;
- `camera.set_gain`, `camera.set_live` and `camera.snap`;
- `incubator.set_setpoint` and `incubator.set_enabled`.

How the stop operations map:
- `xy.stop` and `focus.stop` map to `router.stop_device`.
- STOP maps to `router.stop_all`.

### Where each safety rule now lives

All of these are checked by the SafetyManager:

- **Parameter ranges** (exposure, gain, LED intensity per channel, incubator setpoints, carriage positions). They come from `DeviceCapabilities.ranges` and the related fields; an unknown range is rejected.
- **Operator confirmation** for homing and incubator changes (`CONFIRMATION_REQUIRED`).
- **Cross-device interlocks** (`safety/interlocks.py`). They read **observed state in the store**, never a device:
  - turret moves need the focus below the safe height and idle;
  - the carriage moves only with the epi LEDs off;
  - an epi LED needs its cube in the light path;
  - motion needs a homed axis;
  - no snap while the camera is live.
- **Safe-direction commands** (LEDs off, live view off). They pass the STOP latch, reservations, activity, health and recovery checks. They are **never** exempt from identity, connection or firmware-mode checks: nothing is sent to a bootloader.

### Reservations and recovery

- **Acquisitions** reserve every instrument device except the incubator (`router.reserve`).
- **Their own commands** carry `owner=` and run under that reservation.
- **Anyone else's commands** are rejected with check `exclusive`.
- **Timeouts** keep the reservation and set `recovery_required` until the operator recovers the device. The Devices page shows each device's state, and its Recover button runs `router.recover_device`.

### The adapter is simulation-only

`HalDeviceBackend` wraps the existing simulated HAL devices only. A physical device needs a native `DeviceBackend` that reports `firmware_mode` (ADR-0009). The adapter refuses any device that does not identify as simulated.

## Consequences

- **One validator for the UI.** The UI path is checked by the core's validator alone.
- **The old gateway is retired** (migration stage 7, same day):
  - `hal/gateway.py`, `hal/command_policy.py` and `hal/commands.py` are deleted; the import guard checks they stay gone.
  - `core/command_language.py` keeps only what has no safety role: modes, actors, exceptions, units, parameter specs with static bounds, and the reference catalogue (each entry names its core command kind and the core rules that guard it). It calls no device.
  - The 28 legacy tests were ported to `tests/test_command_port.py` and run on the core path. Check names now come from the core (`limits`, `device_state`, `interlock:<name>`, `recovery`).
- **Fixed while porting:** if STOP arrived while a device call was still in flight, the adapter would have let that move or LED change go ahead. Now, if the command was cancelled during the call, the adapter halts the device again and reports CANCELLED. A command whose cancel races its start never reports success. Test: `test_stop_does_not_wait_for_a_device_call_in_flight`.
- **The command port refreshes the target device's observed state** through the reporter just before admission. A fault the simulator raised on its own (no command running) is therefore seen by validation.
- **Behaviour change: rapid jogs.** A second jog while the stage is still moving is now rejected (device activity is `moving`). It used to retarget the move.
- **The audit trail** records the same events as before. Asynchronous failures of motion that did not wait are recorded when they settle.
- **Tests:**
  - `tests/test_ui_core_migration.py` is the acceptance suite. It covers:
    - the full lifecycle;
    - CANDIDATE limits refused on a physical device;
    - bootloader quarantine;
    - timeout → recovery → resume;
    - STOP preserving the incubator;
    - the interlocks, confirmation, ranges and the firmware refusal.
  - `tests/test_ui_smoke.py` covers the Devices-page core table and recovery.
  - `tests/test_command_port.py` covers the ported gateway guarantees (parameters, units, actors, modes, confirmation, interlocks, execution, STOP, audit).
