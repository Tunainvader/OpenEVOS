# ADR-0009: Firmware safety: no flashing, maintenance-only transitions, bootloader quarantine

- **Status:** Accepted (project owner, 2026-11-12)
- **Related:** ADR-0005 (product direction), ADR-0008 (core execution model), `docs/evidence/first_connection_2026-11-12.md`

## Context

- **On our unit,** the LED board enumerates as `0DAD:0057`, which reports itself as "EVOS Gen 3 v2 LED Board Bootloader". The application normally runs as `0051`.
- **The original PearlScope** compares firmware versions and can flash the LED board, the CN incubator board and the motor drives. The LED board's bootloader interface exposes `DownloadHex`, `VerifyHex`, `StartApp` and `GetApplicationFirmwareVersion`.
- **The LED board also stores stage calibration** (HW_DATA). A wrong flash could lose it.
- **Earlier hand-off notes said this policy existed in code. It did not** (found in the v0.2.0 review). This ADR records the policy, and the tests below enforce it.

## Decision

1. **Flashing ban, with no override.** OpenEVOS never writes, erases, verifies-for-writing or replaces device firmware, and never forces a device into its bootloader. Such operations are refused:
   - by `core.models.parse_command` (`FirmwareOperationRefused`), before parsing;
   - by the Phase 2.1 `SafetyManager`;
   - by the UI command port `CoreGateway` (check `firmware`), before any command exists, and the command catalogue can't contain one (the gateway-era `CommandPolicy` was retired by ADR-0010);
   - by an import-time assertion that no typed command kind is a firmware operation;
   - by a static scan of `core/`, `hal/`, `backends/`, `safety/` and `ui/` for definitions, calls or command literals naming one.

   The vocabulary lives in `openevos.safety.firmware`.
2. **Maintenance-only transitions.** `StartApp`-style operations change state: leaving a bootloader, resetting or rebooting a board. What they do to a board whose application firmware is damaged is unknown. They are refused, with the reason "requires the reviewed maintenance workflow". That workflow does not exist. Creating it needs its own ADR, which must cover at least:
   - documented protocol evidence;
   - a read-only version check first;
   - operator confirmation;
   - an audit record;
   - a stated recovery plan.
3. **Bootloader quarantine.** Every device snapshot carries an observed `firmware_mode` (`application` / `bootloader` / `unknown`), reported by its backend through the same sequence-checked path as other observations.
   - `bootloader` → every command is rejected.
   - `unknown` on a **physical** device → every command is rejected (fail closed). A physical backend must observe and report `application` first.
   - Simulated devices have no firmware; `unknown` is accepted for them.
   - Leaving the bootloader is something OpenEVOS *observes*, never something it commands.

4. **L3 reviewed reads.** Vendor read queries identified statically are not writes, but each is still a command to the device. Its firmware-side behaviour is unknown, because the bootloader image is not in the package. These are `GetFirmwareVersion`, `GetApplicationFirmwareVersion` (command `0x0A`), `GetBoardType`, `GetPersistentData*`, `GetHardwareSpecificData`, the checksum getters and similar. They are refused with an "L3" reason until a reviewed protocol description and explicit operator approval exist (`is_reviewed_read_operation`).

   Calibration writes (`Set*HardwareSpecificData`, `SetPersistentData*`, `SetHardwareIniDataStored`, erase-range and CN5 test-mode setters) are L4 and banned like flashing. See `fl1_v1.6_led_board_static_analysis.md` §5.

## Consequences

- The first physical LED-board backend will be quarantined from the start: the board currently enumerates as `0057`. That's intended.
- Because of the import-time assertion, adding a command type whose name looks like a firmware operation fails at import.
- `tests/test_firmware_safety.py` (56 tests) and `tests/test_core_state_and_safety.py` cover these rules.
