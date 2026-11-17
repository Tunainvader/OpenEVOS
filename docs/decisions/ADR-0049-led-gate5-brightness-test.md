# ADR-0049: LED Gate 5, low-level brightness test

- **Status:** Accepted for implementation: built and tested against a fake board (2026-11-16). Not run on the instrument; each run needs the operator's approval.
- **Spec:** [`fl1_v1.6_led_app_command_table.md` §7](../evidence/fl1_v1.6_led_app_command_table.md)

## Decision

1. **Tool:** `python -m openevos.maintenance.led_brightness_test --light brightfield|epi --level L` (L 0–31, default 2).
2. **Sequence**, each request once, never resent:
   - read the level;
   - set L, read back L;
   - switch the light on, hold (max 10 s), switch it off;
   - restore the original level, read it back.
3. **Requests it can build:**

   | Light | Level set | Level read |
   |---|---|---|
   | Brightfield (PEARL mode) | `0x5E` with field mask byte 2 = `0x04` only, level in byte 19 | `0x5D` byte 35 |
   | Epi | `0x4C` byte 2 | `0x4A` byte 4 |

   - `0x43` / `0x49` on/off as Gate 4.
   - `0x47` to check the cube in the path: epi is refused for an empty slot or DAPI.
4. **Fail safe:**
   - Any failure sends one OFF and one restore of the original level.
   - An unconfirmed OFF or restore tells the operator to power-cycle.
5. **Level chip volatility is Unknown** (an unidentified I2C part). The original level is always restored, and the power-on level is re-read after the next power cycle.
6. Registered in `REVIEWED_MAINTENANCE["led_application.brightness_test"]`.

## Consequences

- A pass with read-backs gives the evidence needed to let the app's brightness slider drive the level (0–100 % → 0–31), in a follow-up change to ADR-0048.
- The byte offsets for the levels (`0x5D` byte 35, `0x4A` byte 4) come from the DLL. The tool stops before any write if the first read is not a 0–31 level with range 0–31.
