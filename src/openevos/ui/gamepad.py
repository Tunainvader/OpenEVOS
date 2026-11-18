"""USB gamepad (XInput) for the Capture page: stage jog, focus, capture, light (operator request, 2026-11-18).

The pad drives the same entry points as the on-screen controls, so every guard they have applies unchanged: the busy
lock-out, one stage axis at a time with a controlled stop on release (ADR-0040), held focus stopping at the objective's
safe height, and the router's interlocks behind them. The pad never changes objective (a turret move needs the stage
centred and Z at 0) and never confirms a move above the safe height on its own: a bumper step past it asks, as a click
on the focus pad does.

    Left stick / D-pad   stage X/Y jog, dominant axis only (stick: pace by deflection; D-pad: medium pace)
    Right stick up/down  focus Z up/down while held (stops when let go)
    RB / LB              one focus step up / down
    A                    Capture        B   Light on/off
    X                    Snap to last   Y   Fine/coarse focus steps
    Start                Quick Save the last capture to file
    Back                 stop: releases every held stage and focus move

A stage direction must be held for JOG_SETTLE_S before the stage starts (a flick or a stick passing through a
diagonal starts nothing; the 2026-11-18 run showed several moves started and stopped inside one 50 ms tick); letting go
stops at once. The on-screen jog knob mirrors the stick, and the focus pad shows the held direction.

Input is read only while the app window is active and no dialog is open; losing the window, opening a dialog or
unplugging the pad releases anything held.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from openevos.input.xinput import PadState, XInputSource

STICK_DEAD = 0.25  # radial dead zone of the left stick (worn sticks rest at up to ~0.15)
FOCUS_DEAD = 0.35  # the right stick must clearly lean before focus moves
FOCUS_FAST = 0.85  # beyond this, a held focus step is doubled
JOG_REPEAT_S = 0.18  # the on-screen joystick's repeat (controls.Joystick)
JOG_SETTLE_S = 0.12  # a new stage direction must be held this long before it starts (stops are immediate)
FOCUS_REPEAT_S = 0.12
DPAD_PACE = 0.6
POLL_MS = 33

#: edge-triggered buttons -> action name
PRESS_ACTIONS = {"a": "capture", "b": "light", "x": "snap", "y": "focus_mode", "rb": "focus_up", "lb": "focus_down",
                 "start": "quick_save", "back": "stop"}


@dataclass
class GamepadMapper:
    """Pure: pad readings -> actions. Actions are tuples:

    ("jog", vx, vy)        stage jog, screen convention (vy < 0 = up = Y+), one axis non-zero
    ("jog_release",)       stick/D-pad back to centre
    ("focus", direction, mult)   one held focus step (direction +1 up / -1 down)
    ("focus_release",)
    (name,)                one of PRESS_ACTIONS' values, on the button's press edge
    """

    _jog_dir: tuple[int, int] | None = None
    _jog_t: float = -1e9
    _cand: tuple[int, int] | None = None  # a new direction waiting out JOG_SETTLE_S
    _cand_t: float = 0.0
    #: the stick/D-pad vector of the last reading (screen convention), for the on-screen knob
    shown: tuple[float, float] = (0.0, 0.0)
    _focus_dir: int = 0
    _focus_t: float = -1e9
    _held: frozenset[str] = field(default_factory=frozenset)

    def release_all(self) -> list[tuple]:
        out: list[tuple] = []
        if self._jog_dir is not None:
            out.append(("jog_release",))
        if self._focus_dir:
            out.append(("focus_release",))
        self._jog_dir, self._focus_dir, self._cand, self.shown = None, 0, None, (0.0, 0.0)
        return out

    def reset(self, state: PadState | None = None) -> list[tuple]:
        """Release everything; buttons already down (e.g. while a dialog was open) do not fire when input resumes."""
        out = self.release_all()
        self._held = state.buttons if state is not None else frozenset()
        return out

    def step(self, s: PadState, now: float) -> list[tuple]:
        out: list[tuple] = []
        # ---- stage: D-pad wins over the stick; dominant axis only (no combined X/Y moves, ADR-0040)
        vx = vy = 0.0
        dpad = [(1, 0) if "right" in s.buttons else (-1, 0) if "left" in s.buttons else None,
                (0, 1) if "up" in s.buttons else (0, -1) if "down" in s.buttons else None]
        if dpad[0] or dpad[1]:
            sx, sy = dpad[1] if dpad[1] else dpad[0]  # type: ignore[misc]
            vx, vy = sx * DPAD_PACE, -sy * DPAD_PACE
        else:
            mag = math.hypot(s.lx, s.ly)
            if mag > STICK_DEAD:
                pace = 0.35 + 0.65 * min(1.0, (mag - STICK_DEAD) / (1 - STICK_DEAD))
                if abs(s.lx) >= abs(s.ly):
                    vx = pace if s.lx > 0 else -pace
                else:
                    vy = -pace if s.ly > 0 else pace  # stick up -> screen up (vy < 0) -> Y+
        self.shown = (vx, vy)
        d = (int(math.copysign(1, vx)) if vx else 0, int(math.copysign(1, vy)) if vy else 0)
        if self._jog_dir is not None and d != self._jog_dir:
            out.append(("jog_release",))  # centred or a new direction: stop at once
            self._jog_dir = None
        if vx or vy:
            if self._jog_dir is None:  # a start waits until the direction has been held JOG_SETTLE_S
                if d != self._cand:
                    self._cand, self._cand_t = d, now
                if now - self._cand_t >= JOG_SETTLE_S - 1e-9:
                    out.append(("jog", vx, vy))
                    self._jog_dir, self._jog_t, self._cand = d, now, None
            elif now - self._jog_t >= JOG_REPEAT_S:
                out.append(("jog", vx, vy))
                self._jog_t = now
        else:
            self._cand = None
        # ---- focus: right stick, vertical only
        fdir = 0 if abs(s.ry) <= FOCUS_DEAD else (1 if s.ry > 0 else -1)
        if fdir:
            if fdir != self._focus_dir or now - self._focus_t >= FOCUS_REPEAT_S:
                if self._focus_dir and fdir != self._focus_dir:
                    out.append(("focus_release",))
                out.append(("focus", fdir, 2 if abs(s.ry) >= FOCUS_FAST else 1))
                self._focus_dir, self._focus_t = fdir, now
        elif self._focus_dir:
            out.append(("focus_release",))
            self._focus_dir = 0
        # ---- buttons, on the press edge
        for b in sorted(s.buttons - self._held):
            if b in PRESS_ACTIONS:
                if PRESS_ACTIONS[b] == "stop":
                    out += self.release_all()
                out.append((PRESS_ACTIONS[b],))
        self._held = s.buttons
        return out


try:  # the Qt bridge; the mapper above stays importable without Qt
    from PySide6.QtCore import QObject, QTimer
    from PySide6.QtWidgets import QApplication
except ImportError:  # pragma: no cover
    QObject = object  # type: ignore[assignment,misc]


class GamepadBridge(QObject):  # type: ignore[misc]
    """Polls the pad on a Qt timer and applies the mapper's actions to the Capture page."""

    def __init__(self, window, source=None, parent=None) -> None:
        super().__init__(parent or window)
        self.win = window
        self.cap = window.pages["capture"]
        self.model = self.cap.model
        self.source = source if source is not None else XInputSource()
        self.mapper = GamepadMapper()
        self.connected = False
        self._live = False  # input accepted on the previous tick
        self._timer = QTimer(self)
        self._timer.setInterval(POLL_MS)
        self._timer.timeout.connect(self.poll)

    def start(self) -> bool:
        if not getattr(self.source, "available", True):
            return False
        self._timer.start()
        return True

    def stop(self) -> None:
        self._timer.stop()
        self._apply(self.mapper.release_all())

    def _accepting(self) -> bool:
        return (self.cap.settings.basic.gamepad_enabled and QApplication.activeWindow() is self.win
                and QApplication.activeModalWidget() is None and QApplication.activePopupWidget() is None)

    def poll(self) -> None:
        try:
            s = self.source.read()
        except Exception:  # noqa: BLE001 - a pad read never takes the app down
            s = None
        if (s is not None) != self.connected:
            self.connected = s is not None
            self.model.add_log("info", "Gamepad connected: left stick/D-pad jog the stage, right stick focuses, "
                                       "A capture, Start quick save, B light, X snap, Y fine/coarse, Back stop"
                               if self.connected else "Gamepad disconnected")
        if s is None or not self._accepting():
            if self._live:
                self._apply(self.mapper.reset(s))
                self.cap.joy.mirror(0.0, 0.0)
            self._live = False
            return
        if not self._live:  # input resumes: buttons already down do not fire
            self.mapper.reset(s)
            self._live = True
        self._apply(self.mapper.step(s, time.monotonic()))
        if self.cap.joy.isEnabled():
            self.cap.joy.mirror(*self.mapper.shown)  # the on-screen knob tilts with the stick

    # ------------------------------------------------------------------ actions -> the Capture page
    def _apply(self, actions: list[tuple]) -> None:
        cap = self.cap
        for a in actions:
            kind = a[0]
            if kind == "jog":
                if cap.joy.isEnabled():
                    cap._jog(a[1], a[2])
            elif kind == "jog_release":
                cap._joy_released()
            elif kind == "focus":
                if cap.focus.isEnabled():
                    cap.focus._stepped(a[1], a[2], True)  # held: stops at the safe height, guards the overshoot
            elif kind == "focus_release":
                cap._focus_hold_end()
            elif kind in ("focus_up", "focus_down"):
                if cap.focus.isEnabled():
                    cap.focus._stepped(1 if kind == "focus_up" else -1, 1, False)
            elif kind == "capture":
                cap.btn_capture.animateClick()  # shows the press; does nothing on a disabled (busy) button
            elif kind == "light":
                cap.btn_power.animateClick()
            elif kind == "snap":
                cap.btn_snap.animateClick()
            elif kind == "quick_save":
                if cap.btn_capture.isEnabled():  # not while a workflow owns the instrument
                    self.win.pages["save"].quick_save()
            elif kind == "focus_mode":
                if cap.focus.isEnabled():
                    mode = "coarse" if cap.focus.mode == "fine" else "fine"
                    cap.focus.set_mode(mode)
                    self.model.add_log("info", f"Focus steps: {mode} ({cap.focus.step_um():g} µm)")
            elif kind == "stop":
                self.model.add_log("info", "Gamepad: stop (held moves released)")
