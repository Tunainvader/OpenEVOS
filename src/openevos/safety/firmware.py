"""Firmware safety policy (ADR-0009): OpenEVOS never flashes; bootloader devices are quarantined.

Evidence behind this policy (docs/evidence/first_connection_2026-11-12.md):
* The LED board on our unit enumerates as `0DAD:0057` "EVOS Gen 3 v2 LED Board Bootloader".
* The original PearlScope compares firmware versions and can flash the LED board, the CN
  (incubator) board and the motor drives; its bootloader interface exposes `DownloadHex`,
  `VerifyHex`, `StartApp`, `GetApplicationFirmwareVersion`.
* The LED board also stores stage calibration (HW_DATA). A wrong flash could lose it.

Three rules, each enforced in code and covered by tests/test_firmware_safety.py:

1. **Flashing ban.** Any operation that writes, erases, verifies-for-writing or replaces device
   firmware, or that forces a device into its bootloader, is refused everywhere: at command
   parsing, in the SafetyManager, and by a static scan of the source tree. There is no override.
2. **Maintenance-only transitions.** `StartApp`-style operations (leaving a bootloader, resetting
   or rebooting a board) mutate device state with unknown consequences if the application
   firmware is damaged. They are refused until a reviewed maintenance workflow exists; none does.
3. **Bootloader quarantine.** A device whose observed firmware mode is BOOTLOADER accepts no
   command at all. A *physical* device whose mode is UNKNOWN is treated the same way (fail closed).

This module imports nothing from `openevos` at module level, so `core.models` can use it to
reject banned command kinds before they are parsed.
"""

from __future__ import annotations

import re

# Normalised (lowercase, alphanumerics only) fragments. A name containing any of them is banned.
_BANNED_FRAGMENTS: tuple[str, ...] = (
    "downloadhex", "downloadblhex", "bootloaderhex", "verifyhex", "programhex", "writehex",
    "flashfirmware", "firmwareflash", "updatefirmware", "firmwareupdate", "upgradefirmware",
    "firmwareupgrade", "reflash", "flashwrite", "writeflash", "eraseflash", "flasherase",
    "resettobootloader", "enterbootloader", "jumptobootloader", "updatebootloader",
    "setupdatedbootloaderversion", "dfu",
    # The LED board's PIC flash holds the instrument calibration (HW_DATA): writes are firmware-class.
    "topicflash", "writehwdata", "hwdatatoflash", "savehwdata", "writeeeprom", "eepromwrite",
    "writecalibration", "calibrationwrite",
    # Exact write methods found in the LED board type libraries (fl1_v1.6_led_board_static_analysis.md §5)
    "sethardwarespecificdata", "setconsecutivelonghardwarespecificdata",
    "setconsecutivedoublehardwarespecificdata", "setconsecutivestringhardwarespecificdata",
    "setpersistentdata", "sethardwareinidatastored", "seteraserange", "setcn5testenabled",
    "setruncn5testunconditionally",
    # Technosoft motor drives (MotorController64.dll -> TML_lib.dll): writing a .sw image or a setup
    # into a drive's EEPROM replaces the drive program/configuration (fl1_v1.6_motor_subsystem_l0.md).
    "downloadswfile", "downloadsw", "savesetup", "writesetup", "eepromdownload",
)
# Plain words that are banned when they appear as a whole name segment (e.g. "led.flash").
_BANNED_SEGMENTS: frozenset[str] = frozenset({"flash", "reflash", "firmware", "dfu"})

_MAINTENANCE_FRAGMENTS: tuple[str, ...] = (
    "startapp", "startapplication", "jumptoapp", "jumptoapplication", "runapp", "leavebootloader",
    "exitbootloader", "resetdevice", "softreset", "hardreset", "rebootdevice", "reboot",
)

# L3: vendor *read* commands identified statically. Not writes, but each is still a command sent to the
# device with firmware-side behaviour we cannot see (the bootloader image is not in the package), so
# each needs a reviewed protocol description and explicit operator approval before any use.
_L3_READ_FRAGMENTS: tuple[str, ...] = (
    "getapplicationfirmwareversion", "getfirmwareversion", "getboardtype", "getpersistentdata",
    "gethardwarespecificdata", "gethardwareinidatastored", "gethardwareinidatadefinitionsversion",
    "gethardwaredatasectionsupported", "geteraserange", "getcn5teststate", "getupdatedbootloaderversion",
)

ADR = "ADR-0009"

#: L3 reads that passed review and exist as operator-run maintenance tools (ADR-0011). The command
#: core still refuses them (`operation_refusal`): only `openevos.maintenance` may send them, once per
#: explicit operator confirmation. tests/test_maintenance_gate1.py checks the tool matches this entry.
REVIEWED_L3_READS: dict[str, dict] = {
    "led_bootloader.version_query": {
        "vid_pid": (0x0DAD, 0x0057), "commands": frozenset({0x0A}), "gate": 1, "adr": "ADR-0011",
        "spec": "docs/evidence/fl1_v1.6_led_board_static_analysis.md §6 Step A",
    },
    "led_application.calibration_checksum": {
        "vid_pid": (0x0DAD, 0x0051), "commands": frozenset({0x59}), "gate": 2, "step": "B", "adr": "ADR-0013",
        "spec": "docs/evidence/fl1_v1.6_led_board_static_analysis.md §5.3, §6 Step B",
    },
    "led_application.calibration_read": {
        "vid_pid": (0x0DAD, 0x0051), "commands": frozenset({0x51}), "gate": 2, "step": "C", "adr": "ADR-0014",
        "fields": "docs/evidence/fl1_v1.6_hwdata_address_map.md (76 fields, 4/8 bytes each)",
        "spec": "docs/evidence/fl1_v1.6_led_board_static_analysis.md §5.3, §6 Step C",
    },
    "led_application.section2_blob_read": {
        "vid_pid": (0x0DAD, 0x0051), "commands": frozenset({0x31}), "gate": 2, "step": "D", "adr": "ADR-0015",
        "layout": "header 0x15713 (4/8/4/4 B), blob 0x15727 in 58-byte chunks, N < 0x88DA",
        "spec": "docs/evidence/fl1_v1.6_led_board_static_analysis.md §5.5a",
    },
    "led_application.gate3_status_read": {
        "vid_pid": (0x0DAD, 0x0051), "commands": frozenset({0x40, 0x47, 0x5A, 0x4A}), "gate": 3, "adr": "ADR-0031",
        "layout": "four bare get requests (no payload), once each, fixed order 0x40, 0x47, 0x5A, 0x4A; stop on any failure",  # noqa: E501
        "spec": "docs/evidence/fl1_v1.6_led_app_command_table.md §4",
    },
    "motor.gate1_read": {
        "vid_pid": (0x0403, 0x6001), "commands": frozenset({0xB000, 0xB004, 0xB005}), "gate": 1, "adr": "ADR-0016",
        "layout": "Technosoft binary at 9600 8N2 (vendor DCB): sync 0xFF, axis-ID query, 22 fixed variable reads",
        "spec": "docs/evidence/fl1_v1.6_motor_gate1_spec.md",
    },
    "motor.gate2_read": {
        "vid_pid": (0x0403, 0x6001), "commands": frozenset({0xB000, 0xB004, 0xB005}), "gate": 2, "adr": "ADR-0017",
        "layout": "Gate 1 rev 2 transport; readiness first, then 41 fixed per-axis reads (limits, CACCSTOP, speeds, positions)",  # noqa: E501
        "spec": "docs/evidence/fl1_v1.6_motor_gate2_spec.md",
    },
    "motor.gate9_status_read": {
        "vid_pid": (0x0403, 0x6001), "commands": frozenset({0xB000, 0xB004, 0xB005}), "gate": 9, "adr": "ADR-0030",
        "layout": "Gate 1 rev 2 transport; 27 fixed 16-bit status reads (MER, SRL, SRH, MSR, ISR, INSTATUS, POSERR, "
                  "ERRMAX, TERRMAX) on X, Y, Z, plus 18 X/Y drive-program variable reads (rev 2); no writes",
        "spec": "docs/evidence/fl1_v1.6_motor_gate9_drive_status_spec.md",
    },
    "motor.gate17_z_o_read": {
        "vid_pid": (0x0403, 0x6001), "commands": frozenset({0xB000, 0xB004, 0xB005}), "gate": 17, "adr": "ADR-0052",
        "layout": "Gate 1 rev 2 transport; 42 fixed reads of Z (axis 4) and O (axis 6) program variables, three times "
                  "each; no writes",
        "spec": "docs/evidence/fl1_v1.6_motor_gates17_19_z_focus_o_turret_spec.md",
    },
}

#: L4 maintenance operations that passed an operator-approved review and exist as operator-run tools
#: (ADR-0012). Still refused by the command core. `module` is the only source file allowed to use the
#: operation's maintenance-only name; the static scan in tests/test_firmware_safety.py enforces that.
REVIEWED_MAINTENANCE: dict[str, dict] = {
    "led_bootloader.start_app": {
        "vid_pid": (0x0DAD, 0x0057), "commands": frozenset({0x08}), "adr": "ADR-0012",
        "module": "maintenance/led_startapp.py",
        "review": "docs/evidence/fl1_v1.6_startapp_l4_review.md",
    },
    "led_application.light_test": {
        "vid_pid": (0x0DAD, 0x0051), "commands": frozenset({0x43, 0x45, 0x49, 0x4A}), "adr": "ADR-0034",
        "writes": "RAM/IO only: 0x43 brightfield enable 0/1, 0x49 epi enable 0/1; intensity/PWM/current unchanged",
        "module": "maintenance/led_light_test.py",
        "review": "docs/evidence/fl1_v1.6_led_app_command_table.md §6",
    },
    "led_application.brightness_test": {
        "vid_pid": (0x0DAD, 0x0051), "commands": frozenset({0x43, 0x49, 0x4A, 0x47, 0x4C, 0x5D, 0x5E}),
        "adr": "ADR-0049", "module": "maintenance/led_brightness_test.py",
        "writes": "RAM/I2C level only: 0x5E with field mask 0x04 (brightfield level) and 0x4C (epi level), 0..31, "
                  "original restored; 0x43/0x49 on/off",
        "review": "docs/evidence/fl1_v1.6_led_app_command_table.md §7",
    },
    "led_application.camera_sweep": {
        "vid_pid": (0x0DAD, 0x0051), "commands": frozenset({0x43, 0x49, 0x4A, 0x47, 0x4C, 0x5D, 0x5E}),
        "adr": "ADR-0050", "module": "maintenance/led_camera_sweep.py",
        "writes": "LED: Gate 5 level/on-off frames only (original level restored). Camera 1E10:2005: Camera Gate 2 "
                  "RAM-only settings (MONO16, gain 0 dB, brightness 2 %, shutter 1/10/50 ms), power-up state restored",
        "review": "docs/evidence/fl1_v1.6_led_app_command_table.md §8",
    },
    "motor.x_controlled_stop_idle": {
        "vid_pid": (0x0403, 0x6001), "commands": frozenset({0x21CD, 0x25E8, 0x21B3}), "adr": "ADR-0018",
        "module": "maintenance/motor_gate3_stop.py",
        "review": "docs/evidence/fl1_v1.6_motor_gate3_stop_spec.md",
    },
    "motor.x_move_1mm_and_back": {
        "vid_pid": (0x0403, 0x6001), "commands": frozenset({0x25D4, 0x21B3, 0x21CD, 0x25E8}), "adr": "ADR-0019",
        "module": "maintenance/motor_gate4_x_move.py",
        "review": "docs/evidence/fl1_v1.6_motor_gate4_x_move_spec.md",
    },
    "motor.x_stop_during_10mm_move": {
        "vid_pid": (0x0403, 0x6001), "commands": frozenset({0x25D4, 0x21B3, 0x21CD, 0x25E8}), "adr": "ADR-0020",
        "module": "maintenance/motor_gate5_x_stop_in_motion.py",
        "review": "docs/evidence/fl1_v1.6_motor_gate5_x_stop_in_motion_spec.md",
    },
    "motor.y_move_12mm_and_back": {
        "vid_pid": (0x0403, 0x6001), "commands": frozenset({0x25D4, 0x21B3, 0x21CD, 0x25E8}), "adr": "ADR-0021",
        "module": "maintenance/motor_gate6_y_move.py",
        "review": "docs/evidence/fl1_v1.6_motor_gate6_y_move_spec.md",
    },
    "motor.xy_load_calibrated_limits": {
        "vid_pid": (0x0403, 0x6001), "commands": frozenset({0x25B4, 0x25B6}), "adr": "ADR-0022",
        "module": "maintenance/motor_gate7_xy_limits.py",
        "review": "docs/evidence/fl1_v1.6_motor_gate7_xy_limits_spec.md",
    },
    "motor.xy_full_range_traverse": {
        "vid_pid": (0x0403, 0x6001), "commands": frozenset({0x25D4, 0x21B3, 0x21CD, 0x25E8}), "adr": "ADR-0028",
        "module": "maintenance/motor_gate8_full_range.py",
        "review": "docs/evidence/fl1_v1.6_motor_gate8_full_range_spec.md",
    },
    "motor.x_speed_presets": {
        "vid_pid": (0x0403, 0x6001), "commands": frozenset({0x25D6, 0x25D4, 0x21B3, 0x21CD, 0x25E8}),
        "adr": "ADR-0032", "module": "maintenance/motor_gate10_speed.py",
        "review": "docs/evidence/fl1_v1.6_motor_gate10_speed_spec.md",
    },
    "motor.f_goto_cube_slot": {
        "vid_pid": (0x0403, 0x6001), "commands": frozenset({0x25B7, 0x21B5}),
        "adr": "ADR-0035", "module": "maintenance/motor_gate11_f_cube.py",
        "review": "docs/evidence/fl1_v1.6_motor_gate11_f_cube_spec.md",
    },
    "motor.f_goto_mirror": {
        "vid_pid": (0x0403, 0x6001), "commands": frozenset({0x25B7, 0x21B5}),
        "adr": "ADR-0041", "module": "maintenance/motor_gate12_f_mirror.py",
        "review": "docs/evidence/fl1_v1.6_motor_gate12_f_mirror_spec.md",
    },
    "motor.f_centring_steps": {
        "vid_pid": (0x0403, 0x6001), "commands": frozenset({0x25B7, 0x21B5}),
        "adr": "ADR-0042", "module": "maintenance/motor_gate13_f_centring.py",
        "review": "docs/evidence/fl1_v1.6_motor_gate13_f_centring_spec.md",
    },
    "motor.f_beyond_mirror_steps": {
        "vid_pid": (0x0403, 0x6001), "commands": frozenset({0x25EC, 0x25B7, 0x21B5}),
        "adr": "ADR-0043", "module": "maintenance/motor_gate14_f_beyond_mirror.py",
        "review": "docs/evidence/fl1_v1.6_motor_gate14_f_beyond_mirror_spec.md",
        "writes": "RAM only: F LPOSMAX (user variable, re-set to 128000 at power-up); always restored to 128000",
    },
    "motor.y_axisoff_idle": {
        "vid_pid": (0x0403, 0x6001), "commands": frozenset({0x0002}),
        "adr": "ADR-0044", "module": "maintenance/motor_gate15_y_axisoff.py",
        "review": "docs/evidence/fl1_v1.6_motor_gate15_y_axisoff_spec.md",
        "writes": "one TML AXISOFF (0x0002) to axis 2 (Y) at idle, at most once per run; no AXISON: Y comes back on "
                  "only through a power cycle",
    },
    "motor.x_t1_runs_y_off": {
        "vid_pid": (0x0403, 0x6001), "commands": frozenset({0x0002, 0x25D6, 0x25D8, 0x25D4, 0x21B3, 0x21CD, 0x25E8}),
        "adr": "ADR-0045", "module": "maintenance/motor_gate8_t1_runs.py",
        "review": "docs/evidence/fl1_v1.6_motor_gate8_rev6_t1_runs_spec.md",
        "writes": "Y: Gate 15's AXISOFF only. X: Gate 10 speed presets, CACCCMD 190/1573/1.0 (RAM, restored), Gate 8 "
                  "targets, move request and Gate 3 stop",
    },
    "led_bootloader.start_app_at_launch": {
        # the same Gate 1 query + StartApp as the two tools above, run by the app's launch path before the command
        # core exists, with the operator's standing approval instead of typed phrases (ADR-0055, amends ADR-0012)
        "vid_pid": (0x0DAD, 0x0057), "commands": frozenset({0x0A, 0x08}), "adr": "ADR-0055",
        "module": "maintenance/led_autostart.py",
        "review": "docs/decisions/ADR-0055-led-board-autostart-at-launch.md",
    },
    "motor.o_turret_goto_position": {
        "vid_pid": (0x0403, 0x6001), "commands": frozenset({0x21CC, 0x21B1, 0x0002}),
        "adr": "ADR-0052", "module": "maintenance/motor_gate19_o_turret.py",
        "review": "docs/evidence/fl1_v1.6_motor_gates17_19_z_focus_o_turret_spec.md",
        "writes": "O (axis 6) only: ITARGETPOS 1..5, ESTATE 3 (GOTO_POS, with the drive's stuck detection), and the "
                  "vendor's AXISOFF on a fault; ESTATE 2/4 and any other value never built; Z must be retracted at 0",
    },
    "motor.z_move_1mm_and_back": {
        "vid_pid": (0x0403, 0x6001), "commands": frozenset({0x25B3, 0x21B1}),
        "adr": "ADR-0052", "module": "maintenance/motor_gate18_z_move.py",
        "review": "docs/evidence/fl1_v1.6_motor_gates17_19_z_focus_o_turret_spec.md",
        "writes": "Z (axis 4) only: CPOSCMD 0, 8200 (1 mm) or 41000 (5 mm), ESTATE 8 (move, drive-clamped) and ESTATE 6 (stop at rest); "  # noqa: E501
                  "speed, acceleration and limits never written",
    },
    "motor.xy_rehome": {
        "vid_pid": (0x0403, 0x6001), "commands": frozenset({0x21B3}),
        "adr": "ADR-0058", "module": "maintenance/motor_gate20_xy_rehome.py",
        "review": "docs/evidence/fl1_v1.6_xy_software_rehome_research.md",
        "writes": "X/Y only: ESTATE 19 (STATE_19_HOME_DOUBLE, the power-up homing routine; PearlScope's own "
                  "XYStageHome frames), one axis at a time, X before Y; RAM only, no variable/limit/speed write, "
                  "no stop while homing; Z must be at home",
    },
}

#: Device writes the APP itself may send (not maintenance tools), each through the router and safety layer.
#: `module` is the only app source file that may build these requests; tests/test_ledboard_illumination.py
#: checks the backend's allowlist equals this entry.
REVIEWED_APP_WRITES: dict[str, dict] = {
    "led_application.app_illumination": {
        "vid_pid": (0x0DAD, 0x0051), "commands": frozenset({0x43, 0x49, 0x45, 0x4A, 0x47, 0x4C, 0x5D, 0x5E}),
        "adr": "ADR-0048", "module": "backends/ledboard/illumination.py",
        "writes": "0x43 brightfield enable 0/1, 0x49 epi enable 0/1 (RAM/IO); 0x5E with field mask 0x04 ONLY = "
                  "brightfield level 0..20 and 0x4C = epi level 0..10, both from the slider (RAM/I2C, Gate 5 "
                  "ADR-0049; power-on levels restored at exit); reads 0x45, 0x4A, 0x47, 0x5D. No PWM or current, "
                  "no StartApp, no persistent or firmware commands",
        "review": "docs/evidence/fl1_v1.6_led_app_command_table.md §6; ADR-0034",
    },
}


def _normalise(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _segments(name: str) -> list[str]:
    # split camelCase, dots, underscores, dashes, spaces
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    return [s for s in re.split(r"[^A-Za-z0-9]+", spaced.lower()) if s]


def is_reviewed_read_operation(name: str) -> bool:
    """L3: a vendor read query that needs protocol review + operator approval before any use."""
    norm = _normalise(name)
    return any(f in norm for f in _L3_READ_FRAGMENTS)


def is_banned_firmware_operation(name: str) -> bool:
    norm = _normalise(name)
    if any(f in norm for f in _BANNED_FRAGMENTS):
        return True
    if is_reviewed_read_operation(name):
        return False  # e.g. GetFirmwareVersion: a read, classified L3, never "flashing"
    return bool(_BANNED_SEGMENTS.intersection(_segments(name)))


def is_maintenance_only_operation(name: str) -> bool:
    norm = _normalise(name)
    return any(f in norm for f in _MAINTENANCE_FRAGMENTS)


def operation_refusal(name: str) -> str | None:
    """Reason an operation name must be refused, or None if the name itself is acceptable."""
    if is_banned_firmware_operation(name):
        return f"'{name}' is a firmware write/flash operation; OpenEVOS never flashes devices ({ADR})"
    if is_maintenance_only_operation(name):
        return (f"'{name}' changes a device's firmware/boot state and requires the reviewed maintenance "
                f"workflow, which does not exist yet ({ADR})")
    if is_reviewed_read_operation(name):
        return (f"'{name}' is an L3 device query: it requires a reviewed protocol description and explicit "
                f"operator approval, which do not exist yet ({ADR})")
    return None


class FirmwareModeRefusal:
    """Bootloader quarantine reasons for a device snapshot (duck-typed to avoid import cycles)."""

    @staticmethod
    def reasons(snapshot) -> list[str]:
        mode = getattr(snapshot.firmware_mode, "value", str(snapshot.firmware_mode))
        kind = getattr(snapshot.backend_kind, "value", str(snapshot.backend_kind))
        if mode in ("bootloader", "recovery"):
            return [f"device is in {mode.upper()} mode: quarantined, no commands accepted ({ADR})"]
        if mode == "unknown" and kind == "physical":
            reason = f"physical device firmware mode is unknown: fail closed until the application is confirmed running ({ADR})"  # noqa: E501
            return [reason]
        return []
