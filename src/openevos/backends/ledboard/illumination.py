"""Real LED illumination for the app: the EVOS Gen III LED board application (0DAD:0051), on/off only (ADR-0048).

The app's light buttons send `SetIllumination(channel, intensity, enabled)` through the router and safety
layer. This backend turns that into the two reviewed on/off writes of LED Gate 4 (ADR-0034), and nothing else:

* `0x43` brightfield PWM enable, value 0/1 — channels "trans" and "phase" (transmitted light).
* `0x49` epi enable, value 0/1 — fluorescence channels. The board lights the cube that is IN THE LIGHT PATH. Before
  epi ON the board's own cube sensor is read (`0x47`, read-only, ADR-0031) and the command is refused unless the cube
  type in the path matches the channel's label. With real motor control the session gives this device a `carriage`
  hook (ADR-0051): if the cube is not in the path, both lights go OFF, the stage service drives F to the cube's slot
  (UNIT_CUBE_SLOTS), and the sensor must then agree before epi goes on. Without the hook F is never moved.
* Brightfield brightness: the slider's 0–100 % -> level 0..BF_LEVEL_AT_100 (of 31) through `0x5E` with field mask `0x04`
  only, read back via `0x5D` (LED Gate 5, ADR-0049). Epi brightness: 0–100 % -> level 0..EPI_LEVEL_AT_100 through
  `0x4C`, read back via `0x4A` byte 4. Both power-on levels are restored at disconnect. PWM and current mode are NEVER
  written. Reads `0x45` / `0x4A` / `0x47` / `0x5D`.

Safety:
* Both lights are switched OFF at connect, on STOP (`halt`), on disconnect / app exit, on any I/O failure and on
  any read-back that does not match. Every OFF must read back 0; if it cannot be confirmed the device reports a
  FAULT telling the operator to power-cycle the instrument.
* Epi (possibly DAPI, near-UV) switches itself off after `epi_max_on_s` (default 300 s).
* The board must already run its application. A board in its bootloader (0DAD:0057) is reported as BOOTLOADER
  (quarantined by the safety layer, ADR-0009); this backend never sends StartApp. Start the board with the
  reviewed maintenance tools first (`led_bootloader`, then `led_startapp`).
* One request at a time on a worker thread, never resent (each exchange: one write, ≤5 passive reads in 1 s).
"""

from __future__ import annotations

import contextlib
import itertools
import queue
import threading
from collections.abc import Callable, Iterable
from typing import Any, Protocol

from openevos.backends.ledboard.protocol import (
    ALLOWED_COMMANDS,
    CMD_BF_ENABLE,
    CMD_EPI_ENABLE,
    CMD_EPI_LEVEL,
    CMD_EPI_STATUS,
    CMD_PEARL_GET,
    CMD_PEARL_SET,
    CMD_POSITIONS,
    LEVEL_MAX,
    LEVEL_MIN,
    PID_APPLICATION,
    PID_APPLICATION_ALT,
    PID_BOOTLOADER,
    REPLY_BUSY,
    REPORT_LEN,
    VID,
    brightfield_level,
    build_request,
    cube_in_path,
    enable_readback,
    epi_level,
    frame_checksum,
)
from openevos.core.models import (
    Activity,
    BackendKind,
    Command,
    Connection,
    DeviceKind,
    FirmwareMode,
    Health,
    Position,
    SetIllumination,
)
from openevos.hal.backend import ExecutionContext, Reporter
from openevos.safety.capabilities import (
    AxisLimits,
    CapabilitySource,
    DeviceCapabilities,
    ParameterRange,
)

HidBackend = Any  # find(vid, pid) / describe(path) / open(path) -> channel (see backends/ledboard/winhid.py)

DEVICE_ID = "led.board"
BRIGHTFIELD_CHANNELS = frozenset({"trans", "phase"})
EPI_MAX_ON_S = 300.0
HALT_WAIT_S = 2.5
#: Slider 100 % = this brightfield level (of 31). Level 10 was verified on the instrument (Gate 5, spec §7.2a);
#: the cap stays well below 31 until power-meter data exists (M8).
BF_LEVEL_AT_100 = 20
#: Slider 100 % = this epi level (of 31). 10 matches the firmware's own cap for some cubes (spec §7.2) and keeps the
#: near-UV DAPI excitation modest; epi levels 1-2 were verified by read-back on RFP (Gate 5, spec §7.2b).
EPI_LEVEL_AT_100 = 10


def _level_for(percent: float, cap: int) -> int:
    if percent <= 0:
        return 0
    return max(1, min(cap, round(percent / 100.0 * cap)))


#: Measured relative brightfield output (mono camera, mean minus dark, 1 ms, F at slot 1) at the levels swept on
#: 2026-11-16 (spec §8.2, led-camera-sweep-20261116T190320-complete). Relative only; levels in between are
#: linearly interpolated. Level 0 = off.
BF_MEASURED_OUTPUT: dict[int, float] = {0: 0.0, 1: 1109.16, 2: 2317.87, 3: 3528.5, 5: 5932.49, 8: 9440.88,
                                        10: 11719.81, 12: 13949.33, 15: 17208.74, 20: 22393.72}


def _interp_output(level: int) -> float:
    pts = sorted(BF_MEASURED_OUTPUT.items())
    for (l0, y0), (l1, y1) in itertools.pairwise(pts):
        if l0 <= level <= l1:
            return y0 + (y1 - y0) * (level - l0) / (l1 - l0)
    return pts[-1][1]


#: relative output of every level 0..BF_LEVEL_AT_100, as a fraction of level BF_LEVEL_AT_100's output
BF_RELATIVE_OUTPUT: tuple[float, ...] = tuple(_interp_output(lv) / _interp_output(BF_LEVEL_AT_100)
                                              for lv in range(BF_LEVEL_AT_100 + 1))


def brightfield_level_for(percent: float) -> int:
    """0–100 % -> the level whose MEASURED output is closest to that percentage of level BF_LEVEL_AT_100's output
    (calibrated slider, spec §8.2). Any non-zero percentage gives at least level 1; 100 % gives BF_LEVEL_AT_100."""
    if percent <= 0:
        return 0
    target = min(percent, 100.0) / 100.0
    best = min(range(1, BF_LEVEL_AT_100 + 1), key=lambda lv: (abs(BF_RELATIVE_OUTPUT[lv] - target), lv))
    return best


def epi_level_for(percent: float) -> int:
    """0–100 % -> level 0..EPI_LEVEL_AT_100; any non-zero percentage gives at least level 1."""
    return _level_for(percent, EPI_LEVEL_AT_100)
IO_TIMEOUT_S = 0.5
MAX_READS = 5
POWER_CYCLE = "LED OFF could not be confirmed: power-cycle the instrument now"
EVIDENCE = ("ADR-0031", "ADR-0034", "ADR-0048", "ADR-0049",
            "led-application-gate5-brightness-20261116T175235-complete",
            "led-application-gate4-light-20261113T173549-complete",
            "led-application-gate4-light-20261115T193028-complete",
            "led-application-gate4-light-20261115T193253-complete")
# cube label in the instrument profile -> Gen3FilterCubeType names the board reports (spec §4b)
LABEL_TO_CUBE = {"DAPI": {"DAPI"}, "GFP": {"GFP"}, "FITC": {"FITC"}, "RFP": {"RFP"}, "TXRED": {"TEXAS_RED"},
                 "TEXASRED": {"TEXAS_RED"}, "TEXAS RED": {"TEXAS_RED"}, "CY5": {"CY5"}, "YFP": {"YFP"},
                 "CFP": {"CFP"}, "MCHERRY": {"MCHERRY"}, "BFP": {"BFP"}}
#: this unit's F carriage loadout (Motor Gate 11: LED Gate 3's 0x47 read at slot 1 + the operator's photo; slot 2
#: empty). Used only to choose where F goes; the board's own 0x47 sensor must then report the cube (ADR-0051).
UNIT_CUBE_SLOTS = {"RFP": 1, "DAPI": 3, "GFP": 4}
#: carriage hook (ADR-0051): dest 'slot1'..'slot4' / 'mirror' -> (ok, message); blocking; supplied by the stage service
CarriageMover = Callable[[str], tuple[bool, str]]


class EpiTimer(Protocol):
    """The epi light's maximum-on timer (threading.Timer, or a test's stand-in): only cancel() is used."""

    def cancel(self) -> None: ...


class LedLinkError(RuntimeError):
    """An exchange with the board failed (no reply, BUSY, bad write, unexpected reply)."""


def present_application_interfaces(backend: HidBackend) -> tuple[list[str], list[str]]:
    """Read-only enumeration (SetupAPI): (application 0051 paths, bootloader 0057 paths). Opens nothing."""
    return backend.find(VID, PID_APPLICATION), backend.find(VID, PID_BOOTLOADER)


class _Link:
    """One open HID handle to the application; one request at a time, never resent."""

    def __init__(self, backend: HidBackend, path: str):
        self._stack = contextlib.ExitStack()
        self._ch = self._stack.enter_context(backend.open(path))

    def close(self) -> None:
        self._stack.close()

    def exchange(self, request: bytes) -> bytes:
        cmd = request[1]
        if cmd not in ALLOWED_COMMANDS:  # belt and braces: the builders already refuse everything else
            raise LedLinkError(f"command 0x{cmd:02X} is not allowed in the app; nothing sent")
        self._ch.flush_input()
        written = self._ch.write(request, IO_TIMEOUT_S)
        if written != REPORT_LEN:
            raise LedLinkError(f"wrote {written} of {REPORT_LEN} bytes")
        want = cmd | 0x80
        for _ in range(MAX_READS):  # passive reads; each limited by IO_TIMEOUT_S
            try:
                r = bytes(self._ch.read(REPORT_LEN, IO_TIMEOUT_S))
            except Exception as exc:
                raise LedLinkError(f"no reply to 0x{cmd:02X}: {exc}") from exc
            if len(r) != REPORT_LEN or r[64] != frame_checksum(r):
                continue
            if r[1] == REPLY_BUSY:
                raise LedLinkError(f"board answered BUSY to 0x{cmd:02X}")
            if r[1] == want:
                return r
        raise LedLinkError(f"no matching reply to 0x{cmd:02X}")


class RealLedIllumination:
    """IlluminationDevice-compatible backend (same surface as the simulator's SimIllumination)."""

    backend_kind = BackendKind.PHYSICAL
    kind = DeviceKind.ILLUMINATION

    def __init__(self, hid: HidBackend, channels: Iterable[str], labels: dict[str, str] | None = None, *,
                 device_id: str = DEVICE_ID, start_channel: str | None = None, epi_max_on_s: float = EPI_MAX_ON_S,
                 timer_factory: Callable[[float, Callable[[], None]], EpiTimer] | None = None):
        self.device_id = device_id
        self.physical_id = "hid:0DAD:0051"  # one owner per physical board
        self.channels = tuple(channels)
        self.labels = {k: (v or "").upper() for k, v in (labels or {}).items()}
        self.channel = start_channel if start_channel in self.channels else self.channels[0]
        self.intensity_percent = 0.0
        self.enabled = False
        self.bf_on = self.epi_on = False
        self.cube_in_path: str | None = None
        self.bf_level: int | None = None  # brightfield level now on the board (0..31)
        self.bf_level_original: int | None = None  # level found at connect; restored on disconnect
        self.epi_level: int | None = None
        self.epi_level_original: int | None = None
        self.epi_max_on_s = epi_max_on_s
        #: F carriage mover (ADR-0051); None = the operator moves F with the maintenance gates
        self.carriage: CarriageMover | None = None
        self._hid = hid
        self._link: _Link | None = None
        self._report: Reporter | None = None
        self._seq = itertools.count(1)
        self._lock = threading.RLock()
        self._jobs: queue.Queue = queue.Queue()
        self._pending: tuple[str, ExecutionContext] | None = None
        self._epi_timer: EpiTimer | None = None
        self._timer_factory = timer_factory or self._thread_timer
        self._worker = threading.Thread(target=self._run, name=f"{device_id}-io", daemon=True)
        self._worker.start()

    # ------------------------------------------------------------------ plumbing
    @staticmethod
    def _thread_timer(delay: float, fn: Callable[[], None]) -> EpiTimer:
        t = threading.Timer(delay, fn)
        t.daemon = True
        t.start()
        return t

    def attach(self, reporter: Reporter) -> None:
        self._report = reporter

    def _obs(self, **fields: Any) -> None:
        if self._report is not None:
            self._report(seq=next(self._seq), **fields)

    def _run(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                return
            fn, done = job
            try:
                fn()
            except Exception as exc:  # noqa: BLE001 - last resort (audit APP-04): keep the worker alive
                # a job that raised outside its own guards used to end this thread, and later light commands were never
                # served. Lights off (confirmed where possible), the command in flight fails, the worker carries on.
                msg = f"LED board worker error: {exc!r}"
                with contextlib.suppress(Exception):
                    msg = self._fail_safe(msg)
                p, self._pending = self._pending, None
                if p is not None:
                    with contextlib.suppress(Exception):
                        p[1].fail(msg, "INTERNAL")
            finally:
                if done is not None:
                    done.set()

    def _submit(self, fn: Callable[[], None], *, wait_s: float | None = None) -> bool:
        done = threading.Event() if wait_s is not None else None
        self._jobs.put((fn, done))
        return done.wait(wait_s) if done is not None else True

    def _state_attributes(self) -> dict:
        return {"channel": self.channel, "led_enabled": self.enabled, "intensity_percent": self.intensity_percent,
                "brightfield_on": self.bf_on, "epi_on": self.epi_on, "cube_in_path": self.cube_in_path,
                "brightfield_level": self.bf_level, "brightfield_level_max_from_slider": BF_LEVEL_AT_100,
                "epi_level": self.epi_level, "epi_level_max_from_slider": EPI_LEVEL_AT_100,
                "intensity_controlled": True}

    # ------------------------------------------------------------------ board I/O (worker thread only)
    def _switch(self, cmd: int, value: int) -> None:
        r = self._link.exchange(build_request(cmd, value))  # type: ignore[union-attr]
        got = enable_readback(cmd, r)
        if got != value:
            raise LedLinkError(f"0x{cmd:02X} read back {got}, commanded {value}")
        if cmd == CMD_BF_ENABLE:
            self.bf_on = bool(value)
        else:
            self.epi_on = bool(value)

    def _read_bf_level(self) -> int:
        r = self._link.exchange(build_request(CMD_PEARL_GET))  # type: ignore[union-attr]
        level, rng = brightfield_level(r)
        if rng != (LEVEL_MIN, LEVEL_MAX) or not LEVEL_MIN <= level <= LEVEL_MAX:
            raise LedLinkError(f"0x5D reports level {level}, range {rng}; expected 0..31 (not in PEARL mode?)")
        return level

    def _set_bf_level(self, level: int) -> None:
        """0x5E field 0x04 only, then read back through 0x5D (Gate 5 sequence, ADR-0049)."""
        if self.bf_level == level:
            return
        self._link.exchange(build_request(CMD_PEARL_SET, level))  # type: ignore[union-attr]
        got = self._read_bf_level()
        self.bf_level = got
        if got != level:
            raise LedLinkError(f"brightfield level read back {got}, commanded {level}")

    def _read_epi_level(self) -> int:
        r = self._link.exchange(build_request(CMD_EPI_STATUS))  # type: ignore[union-attr]
        level, rng = epi_level(r)
        if rng != (LEVEL_MIN, LEVEL_MAX) or not LEVEL_MIN <= level <= LEVEL_MAX:
            raise LedLinkError(f"0x4A reports epi level {level}, range {rng}; expected 0..31")
        return level

    def _set_epi_level(self, level: int) -> None:
        """0x4C, then read back through 0x4A (Gate 5 sequence, ADR-0049)."""
        if self.epi_level == level:
            return
        self._link.exchange(build_request(CMD_EPI_LEVEL, level))  # type: ignore[union-attr]
        got = self._read_epi_level()
        self.epi_level = got
        if got != level:
            raise LedLinkError(f"epi level read back {got}, commanded {level} (firmware cube cap?)")

    def _read_cube_in_path(self) -> str | None:
        r = self._link.exchange(build_request(CMD_POSITIONS))  # type: ignore[union-attr]
        self.cube_in_path = cube_in_path(r)
        return self.cube_in_path

    def _all_off(self) -> list[str]:
        """Both lights OFF, each confirmed by read-back. Returns the lights that could not be confirmed off."""
        self._cancel_epi_timer()
        unconfirmed = []
        for cmd, name in ((CMD_EPI_ENABLE, "epi"), (CMD_BF_ENABLE, "brightfield")):
            try:
                if self._link is None:
                    raise LedLinkError("no link")
                self._switch(cmd, 0)
            except Exception:  # noqa: BLE001 - try the other light too
                unconfirmed.append(name)
        if not unconfirmed:
            self.enabled = False
        return unconfirmed

    def _fail_safe(self, why: str) -> str:
        unconfirmed = self._all_off()
        if unconfirmed:
            msg = f"{why}; {POWER_CYCLE} ({', '.join(unconfirmed)} may be on)"
        else:
            msg = f"{why}; both lights confirmed off"
        self._obs(health=Health.FAULT, activity=Activity.IDLE, attributes=self._state_attributes(), reason=msg,
                  fault=msg)
        return msg

    def _cancel_epi_timer(self) -> None:
        t, self._epi_timer = self._epi_timer, None
        if t is not None:
            with contextlib.suppress(Exception):
                t.cancel()

    def _epi_timeout(self) -> None:
        def job() -> None:
            if not self.epi_on:
                return
            unconfirmed = self._all_off()
            msg = (f"epi switched off automatically after {self.epi_max_on_s:g} s (hold limit, ADR-0048)"
                   if not unconfirmed else f"epi hold limit reached; {POWER_CYCLE}")
            self._obs(health=Health.FAULT if unconfirmed else None, activity=Activity.IDLE,
                      attributes=self._state_attributes(), reason=msg, fault=msg if unconfirmed else None)
        self._submit(job)

    # ------------------------------------------------------------------ lifecycle
    def connect(self) -> None:
        self._obs(connection=Connection.CONNECTING, activity=Activity.INITIALIZING, reason="looking for the LED board")

        def job() -> None:
            try:
                apps, boot = present_application_interfaces(self._hid)
                alt = self._hid.find(VID, PID_APPLICATION_ALT)
                if boot and not apps:
                    self._obs(connection=Connection.DISCONNECTED, firmware_mode=FirmwareMode.BOOTLOADER,
                              health=Health.FAULT, activity=Activity.IDLE,
                              reason="LED board is in its bootloader (0DAD:0057): restart the app, which starts it "
                                     "at launch (ADR-0055), or run python -m openevos.maintenance.led_autostart",
                              fault="LED board in bootloader")
                    return
                if len(apps) != 1 or alt:
                    self._obs(connection=Connection.DISCONNECTED, health=Health.FAULT, activity=Activity.IDLE,
                              reason=f"expected exactly one LED board application 0DAD:0051, found {len(apps)}"
                                     + (" (and 0DAD:0052)" if alt else ""), fault="LED board not found")
                    return
                desc = self._hid.describe(apps[0])
                if (desc.vendor_id, desc.product_id, desc.input_report_len, desc.output_report_len) != \
                        (VID, PID_APPLICATION, REPORT_LEN, REPORT_LEN):
                    self._obs(connection=Connection.DISCONNECTED, health=Health.FAULT, activity=Activity.IDLE,
                              reason="LED board interface does not match 0DAD:0051 with 65/65-byte reports",
                              fault="LED board interface mismatch")
                    return
                self._link = _Link(self._hid, apps[0])
                unconfirmed = self._all_off()  # start dark, whatever the board was doing
                if unconfirmed:
                    raise LedLinkError(f"initial OFF not confirmed for {', '.join(unconfirmed)}")
                self.bf_level = self.bf_level_original = self._read_bf_level()
                self.epi_level = self.epi_level_original = self._read_epi_level()
                self._read_cube_in_path()
                self._obs(connection=Connection.CONNECTED, firmware_mode=FirmwareMode.APPLICATION,
                          health=Health.NORMAL, activity=Activity.IDLE, position=self.read_position(),
                          attributes=self._state_attributes(),
                          reason=f"LED board connected; lights off; cube in path: {self.cube_in_path}")
            except Exception as exc:  # noqa: BLE001
                msg = self._fail_safe(f"LED board connect failed: {exc}")
                self._obs(connection=Connection.DISCONNECTED, reason=msg)

        self._submit(job, wait_s=5.0)

    def disconnect(self) -> None:
        """App exit / unregister: both lights OFF (confirmed) before the handle is closed."""
        def job() -> None:
            p, self._pending = self._pending, None
            if p is not None:
                p[1].fail("LED board disconnecting", "HALTED")
            unconfirmed = self._all_off() if self._link is not None else []
            if self._link is not None and self.bf_level_original is not None and not unconfirmed:
                with contextlib.suppress(Exception):  # put the power-on brightfield level back (light is off)
                    self._set_bf_level(self.bf_level_original)
            if self._link is not None and self.epi_level_original is not None and not unconfirmed:
                with contextlib.suppress(Exception):  # and the power-on epi level
                    self._set_epi_level(self.epi_level_original)
            if self._link is not None:
                with contextlib.suppress(Exception):
                    self._link.close()
                self._link = None
            reason = "disconnected; lights off" if not unconfirmed else f"disconnected; {POWER_CYCLE}"
            self._obs(connection=Connection.DISCONNECTED, activity=Activity.UNKNOWN, reason=reason,
                      health=Health.FAULT if unconfirmed else None, fault=reason if unconfirmed else None)

        self._submit(job, wait_s=5.0)
        self._jobs.put(None)  # stop the worker after the OFF job

    def recover(self) -> None:
        def job() -> None:
            if self._link is None:
                return
            unconfirmed = self._all_off()
            if unconfirmed:
                self._obs(health=Health.FAULT, reason=POWER_CYCLE, fault=POWER_CYCLE)
                return
            self._obs(health=Health.NORMAL, activity=Activity.IDLE, position=self.read_position(),
                      attributes=self._state_attributes(), reason="recovered; lights off")
        self._submit(job)

    # ------------------------------------------------------------------ commands
    def start(self, command: Command, ctx: ExecutionContext) -> None:
        if not isinstance(command, SetIllumination):
            ctx.fail(f"illumination cannot execute {command.kind}", "UNSUPPORTED")
            return
        with self._lock:
            if self._pending is not None:
                ctx.fail("LED board busy", "BUSY")
                return
            self._pending = (command.command_id, ctx)
        ctx.mark_started()
        self._submit(lambda: self._execute(command, ctx))

    def _execute(self, command: SetIllumination, ctx: ExecutionContext) -> None:
        ch, en, pct = command.channel, bool(command.enabled), \
            command.intensity_percent
        try:
            if self._link is None:
                raise LedLinkError("LED board not connected")
            if not en:
                unconfirmed = self._all_off()
                if unconfirmed:
                    raise LedLinkError(f"OFF not confirmed for {', '.join(unconfirmed)}")
            elif ch in BRIGHTFIELD_CHANNELS:
                if self.epi_on:
                    self._cancel_epi_timer()
                    self._switch(CMD_EPI_ENABLE, 0)
                self._set_bf_level(brightfield_level_for(pct))  # the slider; verified by read-back
                if not self.bf_on:
                    self._switch(CMD_BF_ENABLE, 1)
            else:
                cube = self._read_cube_in_path()
                wanted = LABEL_TO_CUBE.get(self.labels.get(ch, ""), set())
                if cube not in wanted and self.carriage is not None:
                    # ADR-0051: dark first, then F to the cube's slot, then the board's sensor must agree
                    slot = next((UNIT_CUBE_SLOTS[c] for c in sorted(wanted) if c in UNIT_CUBE_SLOTS), None)
                    if slot is None:
                        unconfirmed = self._all_off()
                        if unconfirmed:
                            raise LedLinkError(f"OFF not confirmed for {', '.join(unconfirmed)}")
                        self._finish_refused(ctx, ch, cube, why="this instrument has no such cube")
                        return
                    unconfirmed = self._all_off()
                    if unconfirmed:
                        raise LedLinkError(f"OFF not confirmed for {', '.join(unconfirmed)}")
                    self._obs(activity=Activity.MOVING, attributes=self._state_attributes(),
                              reason=f"moving the filter carriage to slot {slot} ({self.labels.get(ch, ch)})")
                    ok, msg = self.carriage(f"slot{slot}")
                    if not ok:
                        self.channel, self.enabled = ch, False  # this channel, dark: the UI's toggle follows
                        self._obs(activity=Activity.IDLE, position=self.read_position(),
                                  attributes=self._state_attributes(), reason=msg)
                        self._finish(ctx, ok=False, message=f"filter carriage: {msg}; all lights are off",
                                     code="CARRIAGE_FAILED")
                        return
                    cube = self._read_cube_in_path()
                if cube not in wanted:  # refuse, and leave the instrument dark rather than in its old state
                    unconfirmed = self._all_off()
                    if unconfirmed:
                        raise LedLinkError(f"OFF not confirmed for {', '.join(unconfirmed)}")
                    self._finish_refused(ctx, ch, cube)
                    return
                if self.bf_on:
                    self._switch(CMD_BF_ENABLE, 0)
                self._set_epi_level(epi_level_for(pct))  # the slider; verified by read-back
                if not self.epi_on:
                    self._switch(CMD_EPI_ENABLE, 1)
                self._cancel_epi_timer()
                self._epi_timer = self._timer_factory(self.epi_max_on_s, self._epi_timeout)
        except Exception as exc:  # noqa: BLE001 - link failure or wrong read-back: fail safe
            msg = self._fail_safe(f"LED board: {exc}")
            self._finish(ctx, ok=False, message=msg)
            return
        self.channel, self.intensity_percent, self.enabled = ch, pct, en
        self._obs(activity=Activity.IDLE, position=self.read_position(), attributes=self._state_attributes(),
                  reason=f"{ch} {'on (board default level)' if en else 'off'}")
        self._finish(ctx, ok=True)

    def _finish_refused(self, ctx: ExecutionContext, ch: str, cube: str | None, why: str = "") -> None:
        label = self.labels.get(ch, ch)
        hint = (why or "the board's cube sensor disagrees after the carriage move") if self.carriage is not None \
            else "move the filter carriage first (needs --motor-control)"
        msg = (f"{label or ch} needs its cube in the light path, but the board reports {cube or 'no cube'}; "
               f"{hint}. All lights are off")
        self.channel, self.enabled = ch, False  # this channel, dark: the UI's Light toggle follows the refusal
        self._obs(activity=Activity.IDLE, position=self.read_position(), attributes=self._state_attributes(),
                  reason=msg)
        self._finish(ctx, ok=False, message=msg, code="CUBE_NOT_IN_PATH")

    def _finish(self, ctx: ExecutionContext, *, ok: bool, message: str = "", code: str = "LED_FAULT") -> None:
        with self._lock:
            if self._pending is None or self._pending[1] is not ctx:
                return
            self._pending = None
        if ok:
            ctx.complete(self.read_position())
        else:
            ctx.fail(message, code)

    def cancel(self, command_id: str) -> None:
        """A command executes in well under a second and is never half-applied; switching off is the
        operator's cancel (`halt`)."""
        return

    def halt(self) -> None:
        """Operator STOP: both lights OFF, confirmed by read-back. Waits at most HALT_WAIT_S for the OFF (an
        exchange already in progress finishes first, ≤1 s), so STOP returns with the lights off in practice."""
        def job() -> None:
            if self._link is None:
                return
            unconfirmed = self._all_off()
            if unconfirmed:
                self._obs(health=Health.FAULT, activity=Activity.IDLE, attributes=self._state_attributes(),
                          reason=f"halted; {POWER_CYCLE}", fault=POWER_CYCLE)
            else:
                self._obs(activity=Activity.IDLE, position=self.read_position(), attributes=self._state_attributes(),
                          reason="halted: lights off")
        self._submit(job, wait_s=HALT_WAIT_S)

    # ------------------------------------------------------------------ state
    @property
    def busy(self) -> bool:
        return self._pending is not None

    @property
    def emitting(self) -> bool:
        return self.bf_on or self.epi_on

    def read_position(self) -> Position | None:
        return Position(axes=("carriage",), values=(float(self.channels.index(self.channel)),), unit="index",
                        frame=self.device_id)

    def verify_result(self, command: Command) -> tuple[bool, str]:
        if isinstance(command, SetIllumination):
            want = bool(command.enabled)  # 0 % = on at level 0 (zero intensity)
            if self.enabled != want:
                return False, "LED state does not match the command"
            if want and command.channel in BRIGHTFIELD_CHANNELS and not self.bf_on:
                return False, "brightfield did not read back on"
            if want and command.channel not in BRIGHTFIELD_CHANNELS and not self.epi_on:
                return False, "epi did not read back on"
        return True, ""

    def capabilities(self) -> DeviceCapabilities:
        return DeviceCapabilities(
            device_id=self.device_id, backend_kind=BackendKind.PHYSICAL, source=CapabilitySource.VERIFIED,
            supported_commands=frozenset({"illumination.set"}), illumination_channels=self.channels,
            ranges=tuple(ParameterRange(name=f"intensity:{ch}", limits=AxisLimits(minimum=0.0, maximum=100.0,
                                                                                   unit="%"))
                         for ch in self.channels),
            evidence_ids=EVIDENCE,
            note="Real LED board (0DAD:0051), on/off only at the board's default level; epi lights the cube in "
                 "the light path; auto-off after the epi hold limit (ADR-0048).")
