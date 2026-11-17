# OpenEVOS

Open-source diagnostics, and later control, for the **original EVOS FL Auto (AMAFD1000)** and the **Onstage Incubator (AMC1000)**.

> **Status: v0.1, Inspector only.** OpenEVOS v0.1 does not control the microscope. It is a read-only tool that inventories a Windows PC, the devices attached to it and any recovered EVOS software, so that the first integration target can be chosen from evidence rather than guesswork. No EVOS hardware interface has been verified yet.

## What the Inspector does

| Collector | OS | What it reads (read-only) |
|---|---|---|
| `system` | Windows | OS, board, BIOS, CPU, GPU, disks, USB controllers, .NET release |
| `windows_devices` | Windows | Every PnP device, **including non-present ones** (Windows remembers devices that were attached before), plus parent/child topology, the USB *bus-reported* product string and driver properties |
| `serial_ports` | Windows | COM-port metadata. **Ports are never opened.** |
| `drivers` | Windows | Drivers bound to devices (`Win32_PnPSignedDriver`): INF, provider, version, signature |
| `driver_packages` | any | Third-party `oem*.inf` packages on the live system **and on a mounted copy of the recovered disk image**, with every `USB\VID_xxxx&PID_yyyy` each package binds to |
| `installed_software` | Windows | Uninstall entries, plus EVOS-related vendor registry key *names* |
| `evos_artifacts` | any | EVOS-related files: size, SHA-256, **PE bitness (incl. .NET 32BITREQUIRED)**, version resources, Authenticode status. Files are never copied. |
| `camera_sdks` | any | Known camera-SDK and USB-bridge DLLs (filename heuristics, strengthened by version resources) and Micro-Manager `mmgr_dal_*` adapters |
| `event_logs` | Windows | Kernel-PnP / UserPnp / DriverFrameworks events for a chosen time window |

Every value in the exports links to an evidence item, which records its source, timestamp, collector version and SHA-256. Every claim carries one of six labels: *Verified, Strongly supported, Probable, Unknown, Not tested, Not applicable*. See [ADR-0003](docs/decisions/ADR-0003-ai-evidence-boundary.md).

### Outputs (one folder per scan)

`system_inventory.json`, `device_inventory.json`, `driver_inventory.json`, `software_inventory.json`, `evidence_register.json`, `scan_manifest.json`, `openevos_diagnostic_report.md`

## Install (replacement Windows PC)

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[dev]"
python -m pytest              # mocked suite; also runs the live read-only query smoke tests on Windows
openevos-inspector collectors # lists every collector and every PowerShell query it may run
```

Run as a **standard (non-administrator) user**. No query needs elevation.

## The discovery workflow

```powershell
# 1. Baseline: microscope and incubator disconnected
openevos-inspector scan --label baseline

# 2. Microscope only: powered, documented USB cable, SAME USB port every time
openevos-inspector scan --label microscope
openevos-inspector diff openevos-scans\baseline-<id> openevos-scans\microscope-<id>

# 3. AMC1000 only (microscope unplugged)
openevos-inspector scan --label amc1000
openevos-inspector diff openevos-scans\baseline-<id> openevos-scans\amc1000-<id>
```

### Recovered OptiPlex disk (after it has been imaged — see the plan, §10.1)

Mount a **copy** of the image read-only and point the Inspector at it. This works on Linux or Windows:

```bash
# Linux example: never mount the original disk; attach a COPY of the image read-only
sudo losetup --find --show --read-only --partscan evos_hdd_copy.img   # prints e.g. /dev/loop0
lsblk /dev/loop0                       # Win7-era disks often have a ~100 MB "System Reserved" p1;
sudo mount -o ro,noexec /dev/loop0p2 /mnt/evos   # mount the large NTFS partition that contains \Windows
openevos-inspector scan --label recovered-image --artifact-root /mnt/evos --out ~/evos-scans
```

Then cross-reference the newly enumerated devices against the original EVOS driver packages:

```powershell
openevos-inspector diff <baseline> <microscope> --recovered <recovered-image-scan>
```

The Inspector refuses to write its output inside an `--artifact-root`.

### Sharing a scan

```powershell
openevos-inspector scan --label baseline --redact --redact-value "My Institute"
```

`--redact` replaces the computer name, device serial numbers and user names with stable keyed tokens. Redacted scans made with the same key can still be diffed.

### Validating an AI response

```powershell
openevos-inspector validate-ai response.json --scan openevos-scans\microscope-<id>
```

A response is rejected if it labels anything "verified", cites evidence that does not exist, claims a component "is working" without stage ≥ Responsive, or recommends an action that sends data, moves hardware or changes drivers or disks.

## Repository layout

```text
src/openevos/
  inspector/            # v0.1 deliverable
    safe_exec.py        # the ONLY subprocess entry point: allowlisted read-only PowerShell
    evidence.py         # labels, confidence bands, stages, findings, register
    schemas.py          # export file schemas
    pe.py               # PE/.NET bitness parser (pure Python)
    collectors/         # one module per evidence source
    analysis/rules.py   # deterministic findings
    analysis/diff.py    # snapshot comparison
    reports/            # JSON + Markdown export, redaction
    data/               # SDK signature table, documented EVOS profile (no unverified IDs)
  ai/                   # response schema, register validation, provider interface (NullProvider)
  hal/status_model.py   # runtime state (separate axis from evidence stage; ADR-0004)
docs/decisions/         # ADR-0001 … ADR-0027 (0005 product direction; 0006 STOP keeps the incubator
                        #   running; 0007 runtime state axes; 0008 core execution model; 0009 firmware
                        #   safety; 0010 the desktop UI runs on the command core; 0011 Gate 1 LED
                        #   bootloader version query; 0012 LED StartApp maintenance tool; 0013 Gate 2
                        #   Step B calibration checksum; 0014 Gate 2 Step C HW_DATA read;
                        #   0015 Gate 2 Step D section-2 calibration blob; 0016 Motor Gate 1 read-only drive contact; 0017 Motor Gate 2 safety-variable reads; 0018 Motor Gate 3 idle X controlled stop; 0019 Motor Gate 4 X 1 mm move; 0020 Motor Gate 5 stop during motion; 0021 Motor Gate 6 Y 12 mm move; 0022 Motor Gate 7 calibrated X/Y limits; 0023 read-only motor telemetry in the app; 0024 physical X/Y stage Stop and jog; 0025 UI manual parity; 0026 Craftimon design system; 0027 v0.4.0 UI merge)
tests/                  # mocked Windows fixtures, read-only AST guard, opt-in live Windows tests
```

*Layout note:* plan §9.2 shows a top-level `openevos_inspector/` package, while §14 puts the Inspector under `src/openevos/inspector/`. This repository follows §14, so nothing has to be renamed when the HAL and UI arrive.

## AI assistant link (MCP, read-only)

`openevos --mcp` serves a read-only [MCP](https://modelcontextprotocol.io) link inside the running app, so an assistant such as Claude Code can see what the operator sees: positions, device health, events and faults, the X/Y drift monitor, and downsampled images. It can command nothing (ADR-0003). The link listens on `127.0.0.1` only, needs a bearer token and refuses browser origins. The app prints the connect line on start:

```
pip install -e ".[ui,mcp]"
openevos --mcp            # http://127.0.0.1:8765/mcp
claude mcp add --transport http openevos http://127.0.0.1:8765/mcp --header "Authorization: Bearer <token>"
```

`openevos-mcp` runs the same tools on a headless simulator (stdio by default, `--http` for loopback HTTP) and never opens hardware. Tools: `describe_api`, `get_state`, `get_events`, `probe`, `stage_monitor`, `list_images`, `get_image`. See [ADR-0054](docs/decisions/ADR-0054-read-only-mcp-link.md).

## Phase 2.1 command core (merged from v0.2.0)

`openevos.core` now contains a headless, Qt-free command core: typed commands, a strict lifecycle (QUEUED → VALIDATING → ACCEPTED → RUNNING → SUCCEEDED, with REJECTED / FAILED / CANCELLED / TIMED_OUT), atomic validation + resource reservation, read-back-verified motion, three-axis device snapshots, capability provenance (simulated / candidate / verified) and a lossless scientific frame path. See [ADR-0007](docs/decisions/ADR-0007-runtime-state-axes.md) and [ADR-0008](docs/decisions/ADR-0008-core-execution-model.md). Firmware safety ([ADR-0009](docs/decisions/ADR-0009-firmware-safety.md)): OpenEVOS never flashes; a device in bootloader mode, or a physical device with unknown firmware mode, accepts no command.

**The desktop UI runs on this core** ([ADR-0010](docs/decisions/ADR-0010-ui-on-command-core.md)). Every button, channel change, snap and acquisition step becomes a typed command and goes through the router: STOP latch → safety checks (state, firmware mode, limits, confirmation, cross-device interlocks on observed state) → atomic reservation → readback-verified completion. The Devices page shows each device's connection, health, activity, firmware mode and recovery state, with a Recover button. The old `InstrumentGateway` and its policy are retired; `core/command_language.py` keeps only the named-command vocabulary (units, parameters, reference catalogue).

## Maintenance reads (operator-run, outside the application)

`python -m openevos.maintenance.led_bootloader` is Gate 1 ([ADR-0011](docs/decisions/ADR-0011-gate1-led-bootloader-version-query.md)): one version query (`0x0A`) to the LED board bootloader `0DAD:0057`. It is a dry run unless given `--send --confirm "SEND 0A TO 0DAD:0057"`; it sends exactly one 65-byte report, never retries, and writes the raw bytes to `openevos-scans/`. The desktop app cannot reach it.

`python -m openevos.maintenance.led_startapp` starts the LED board application ([ADR-0012](docs/decisions/ADR-0012-led-startapp-maintenance-tool.md)): one `0x08` report, the same one the vendor software sends every session. It needs a Gate 1 record under 30 minutes old for the same device, plus `--send --checklist "STAGE EMPTY, LID CLOSED, OBSERVER PRESENT" --confirm "START LED APP ON 0DAD:0057"`. Afterwards it only watches for `0DAD:0051` to appear (30 s); it never sends anything to the application.

`python -m openevos.maintenance.led_checksum` is Gate 2 Step B ([ADR-0013](docs/decisions/ADR-0013-gate2-stepb-calibration-checksum.md)): one `0x59` query to the started application, which reports the stored and computed checksums of the calibration memory, with no calibration values. Dry run unless given `--send --confirm "SEND 59 TO 0DAD:0051"`.

`python -m openevos.maintenance.led_calibration_read` is Gate 2 Step C ([ADR-0014](docs/decisions/ADR-0014-gate2-stepc-hwdata-read.md)). It reads the 76 mapped `HW_DATA` fields one `0x51` request at a time, at the vendor's 4/8-byte sizes, and stops at the first problem. It needs a consistent Step B record under 30 minutes old. Values go to a git-ignored file as CANDIDATE data with unknown units. (On our unit all 76 read zero: PearlScope actually keeps the calibration elsewhere, see Step D.)

`python -m openevos.maintenance.led_section2_read` is Gate 2 Step D ([ADR-0015](docs/decisions/ADR-0015-gate2-stepd-section2-blob.md)). It reads PearlScope's compressed calibration blob from section 2 with `0x31` reads: first the 4-field header (magic "lico", length), optionally stopping there with `--header-only`, then 58-byte chunks. It fails fast and decodes offline. Confirmation phrase: `READ SECTION 2 BLOB FROM 0DAD:0051`. The decoded stream is parsed offline by `python -m openevos.maintenance.pearl_persistent` ([schema](docs/evidence/fl1_v1.6_pearl_persistent_schema.md)); keep its output in `openevos-scans/`.

`python -m openevos.maintenance.motor_gate1` is Motor Gate 1 ([ADR-0016](docs/decisions/ADR-0016-motor-gate1-read-only.md), [spec](docs/evidence/fl1_v1.6_motor_gate1_spec.md)). Spec revision 2: `--inspect-port` (phrase `INSPECT MOTOR PORT COMn`) only reads back the port settings. `--send` sets up the port exactly like the vendor (read-back DCB with DTR/RTS kept; refused unless the driver is in its normal state), syncs at 115,200 then 9,600, checks the gateway axis ID, then sends 22 fixed variable reads (state, homing marker, script version, position), each once and each after its own line sync, stopping on any problem or if a drive is not idle. Reads only; the baud change and axis-ID write cannot be built. `--send` requires `--instrument-on-2min` and the phrase `READ MOTOR DRIVES ON COMn`; dry run by default.

`python -m openevos.maintenance.motor_gate2` is Motor Gate 2 ([ADR-0017](docs/decisions/ADR-0017-motor-gate2-safety-variables.md), [spec](docs/evidence/fl1_v1.6_motor_gate2_spec.md)). On the same transport as Gate 1 rev 2 it checks that every drive is idle and homed, then reads 41 fixed per-axis variables (`LPOSMIN`/`LPOSMAX`, `CACCSTOP`, `CSPDCMD`/`CACCCMD`, `APOS`/`CPOS`) and reports whether the drives' own limits and stop deceleration are meaningful. Reads only. Requires `--instrument-on-2min` and the phrase `READ MOTOR LIMITS ON COMn`; dry run by default.

`python -m openevos.maintenance.motor_gate3_stop` is Motor Gate 3 ([ADR-0018](docs/decisions/ADR-0018-motor-gate3-idle-x-stop.md), [spec](docs/evidence/fl1_v1.6_motor_gate3_stop_spec.md)), the first write to a drive: after readiness and an X baseline check, it sends three constant frames to the idle X axis (`IPENDINGMOVES` = 1, `CACCSTOP` = 1.0 as read, `ESTATE` = 6, a controlled stop), checks each `O` acknowledgement, and verifies X returns to idle with nothing changed. No other write can be built. Requires `--instrument-on-2min`, `--stage-clear-watching` and the phrase `STOP IDLE X AXIS ON COMn`; dry run by default.

`python -m openevos.maintenance.motor_gate4_x_move` is Motor Gate 4 ([ADR-0019](docs/decisions/ADR-0019-motor-gate4-x-move-1mm.md), [spec](docs/evidence/fl1_v1.6_motor_gate4_x_move_spec.md)), the first movement: from home, it moves X to +2,268 counts (1.000 mm) and back to 0 at the drive's power-up speed, checking completion (`SRH` bit 9 and state 13) and the encoder position after each leg, then stops X back to idle. Writes only constant targets, the move request and Gate 3's stop; on any fault it sends one stop and nothing else. Requires `--instrument-on-2min`, `--stage-empty-watching` and the phrase `MOVE X 1MM AND BACK ON COMn`; dry run by default.

`python -m openevos.maintenance.motor_gate5_x_stop_in_motion` is Motor Gate 5 ([ADR-0020](docs/decisions/ADR-0020-motor-gate5-x-stop-in-motion.md), [spec](docs/evidence/fl1_v1.6_motor_gate5_x_stop_in_motion_spec.md)): it commands X towards +10 mm, sends the verified controlled stop 0.5 s in, checks X came to rest short of the target, then returns to 0 and stops to idle. Requires `--instrument-on-2min`, `--stage-empty-watching` and the phrase `STOP X DURING 10MM MOVE ON COMn`; dry run by default.

`python -m openevos.maintenance.motor_gate6_y_move` is Motor Gate 6 ([ADR-0021](docs/decisions/ADR-0021-motor-gate6-y-move-12mm.md), [spec](docs/evidence/fl1_v1.6_motor_gate6_y_move_spec.md)): Gate 4's sequence on Y, +27,213 counts (12 mm) and back to 0, with Y-only constant frames. Requires `--instrument-on-2min`, `--stage-empty-watching` and the phrase `MOVE Y 12MM AND BACK ON COMn`; dry run by default.

`python -m openevos.maintenance.motor_gate7_xy_limits` is Motor Gate 7 ([ADR-0022](docs/decisions/ADR-0022-motor-gate7-xy-calibrated-limits.md), [spec](docs/evidence/fl1_v1.6_motor_gate7_xy_limits_spec.md)): it reads the backed-up X/Y position limits from the git-ignored parsed calibration, validates them against the known spans, and writes them to the drives' `LPOSMIN`/`LPOSMAX` (as PearlScope does at start-up), reading each back. No motion; RAM only. Requires `--instrument-on-2min`, `--stage-clear` and the phrase `LOAD XY CALIBRATED LIMITS ON COMn`; dry run by default.

**Desktop app (v0.5, [ADR-0027](docs/decisions/ADR-0027-ui-merge-v040.md)).** `python -m openevos.ui` (or `openevos`) starts the manual-parity UI merged from the v0.4.0 line (Craftimon design, [ADR-0025](docs/decisions/ADR-0025-ui-manual-parity.md)/[ADR-0026](docs/decisions/ADR-0026-craftimon-design-system.md)): Capture, Scan, Time-lapse, Z-stack, Movies, Save, Review, Analysis, Hardware, Environment, Calibration, Settings, Service, Diagnostics (Inspector scans) and Event log, on simulated optics. `--motor-telemetry COM3|auto|sim` adds the real motor drives read-only (ADR-0023); `--motor-control` makes the Capture joystick, the Hardware page's fixed 0.1/1/5 mm steps and Stop act on the **real X/Y stage** inside the 0-22 mm power-up box, through the safety checks, with every command appended to `physical_audit.jsonl` (ADR-0024). The badge then reads SIM + PHYSICAL X/Y STAGE; vessel-map moves are disabled for the physical stage (its vessel mapping is not calibrated). `--screenshot-dir DIR` renders every workspace offscreen. Install with `pip install -e .[ui]` (adds scipy, scikit-image, tifffile, Pillow, PySide6).

## Vendor-software evidence

`docs/evidence/vendor_software_analysis.md` records a static analysis of the FL Auto 1.6 and FL Auto 2 installers. Candidate USB IDs from it live in `src/openevos/inspector/data/candidate_ids.json`, and `diff` uses them to suggest roles (never *Verified*).

## Known limitations of v0.1

- Validated on one real host so far: the replacement EVOS PC (Dell OptiPlex 9020, Windows 11 Pro N build 26300, Windows PowerShell 5.1), 2026-11-12, with the microscope and AMC1000 **not** connected. The full suite, including `pytest -m windows`, passes there, and a live scan completes with every collector `ok`. On any other PC, run `pytest -m windows` first and report any query that fails.
- That first real run found three bugs the mocked fixtures could not show. All three are fixed and have regression tests in `tests/test_environment.py`:
  1. `dict(os.environ)` has UPPERCASE keys on Windows, so the default EVOS and camera-SDK search folders were silently dropped.
  2. In Windows PowerShell 5.1, a calculated property with no `else` serialises as `{}`. That made the whole `drivers` collector fail.
  3. The keyword `evos` matched `openevos`, so the tool reported its own files as EVOS remnants.
- No offline registry-hive parsing yet (see "Next" below).
- The USB descriptors read are those Windows exposes through PnP properties. Endpoint/interface descriptors would need a separate, user-approved tool.
- SDK detection uses filenames. A match means "this file exists", not "the EVOS uses it".

## Next (not in v0.1)

1. **Offline hive reader** for the recovered image's `SYSTEM` hive (`ControlSet00x\Enum\USB`, `Enum\FTDIBUS`, …), which would list every USB device ever attached to the original EVOS PC. This could give the microscope's VID/PIDs without connecting it.
2. PySide6 inventory and report views (Phase 2), reading the same JSON.
3. AI provider adapters behind `AIProvider`, with a redaction preview UI.

## Legal

Do not commit proprietary EVOS binaries, drivers, disk images, calibration files or unredacted scans. `.gitignore` blocks the common formats. Choose a licence before the first public release.
