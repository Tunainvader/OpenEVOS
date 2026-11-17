# ADR-0012: StartApp on the LED board, as an operator-run maintenance tool

- **Status:** Accepted: implemented and tested against a fake device (2026-11-12). With separate operator approval it was run once on the instrument the same day: one `00 08` report, and the application (`0DAD:0051`, 65/65-byte reports) appeared after about 1 s. Nothing has been sent to the application. See §8 of the review.
- **Builds on:**
  - ADR-0009: firmware safety, L4 maintenance-only operations;
  - ADR-0011: Gate 1;
  - the [StartApp L4 review](../evidence/fl1_v1.6_startapp_l4_review.md).

## Context

Our LED board enumerates as its bootloader, `0DAD:0057`. The Gate 2 reads (checksum and calibration) need its application, `0DAD:0051`. The only way there is `StartApp`, one 65-byte report `00 08…`, which ADR-0009 classes as L4, maintenance-only.

The review found that the vendor software sends exactly this report to this board as the first LED-board traffic of **every** session. So the bootloader is most likely the board's normal power-on state.

## Decision

`python -m openevos.maintenance.led_startapp` implements §5 of the review, and nothing more.

### Preconditions
Any failure means nothing is sent:
- Exactly one `0DAD:0057` interface is present, and no `0051`/`0052`.
- The driver reports `0DAD:0057` with 65/65-byte reports.
- There is a Gate 1 reply record for the same device interface, at most 30 minutes old, with a valid reply and a non-blank application version.
- The operator gives two exact phrases:
  - checklist: `STAGE EMPTY, LID CLOSED, OBSERVER PRESENT`;
  - confirmation: `START LED APP ON 0DAD:0057`.

### Action
- One `WriteFile` of the constant `00 08` followed by 63 zero bytes, with a 500 ms limit.
- No flush and no read, matching the vendor. Never retried.
- The only report the module can build is `0x08`.

### Afterwards
- Enumeration only, once a second for up to 30 s.
- It records when `0057` disappears and when `0051`/`0052` appears.
- **Nothing is ever sent to the application.**
- If the application doesn't appear, the tool says so and stops. The return path is a power cycle.

### Evidence
A JSON record in `openevos-scans/` (git-ignored) for every dry run or attempt. It holds:
- the request bytes;
- the device lists before and after;
- the timings.

### Policy
- `safety/firmware.py` lists the operation in `REVIEWED_MAINTENANCE`, naming `maintenance/led_startapp.py` as its **only** permitted source file.
- The static firmware scan exempts maintenance-class names (such as `start_app`) in that file alone. Flash and write names stay banned there too.
- The command core still refuses `start_app` (`operation_refusal`).
- The import guard keeps `openevos.maintenance` out of `core/`, `hal/`, `ui/` and `backends/`.

## Consequences

- **Running it is a separate approval.** The recommended sequence:
  1. Gate 1 (again, if the last record is older than 30 min);
  2. a StartApp dry run;
  3. the physical checklist;
  4. one send.
- **After a successful start**, the board is in its application until power-off. Nothing in OpenEVOS talks to it then:
  - the command core keeps a physical device with unknown firmware mode fail-closed;
  - the Gate 2 reads need their own approval and their own tool.
- **Unchanged:**
  - the flashing ban;
  - the ban on the application's return-to-bootloader;
  - the ban on `HW_DATA` writes.
- **Tests:**
  - `tests/test_maintenance_startapp.py` covers the exact report, a single write with no read, enumeration-only afterwards, every precondition (Gate 1 age, device and phrases), the no-application and write-failure paths, and the evidence;
  - `tests/test_firmware_safety.py` covers the narrowed exemption.
