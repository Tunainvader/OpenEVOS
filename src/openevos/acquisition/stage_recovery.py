"""Automatic recovery after an X/Y drive trip: the app side of ADR-0067. Qt-free and blocking (not on the UI thread).

The stage service (`backends/technosoft/telemetry.py`) decides whether a lock may be recovered, owns the one attempt
per axis per session, and does everything on the drives. This module does what only the app can: switch the light
off, lower the objective to Z 0 through the router (the focus device's checks apply), hand over to the service, and
raise the objective back once the stage is back. Every step goes into the service's recovery record
(`stage-recovery-*.json`) and the stage monitor's log.

A Multi-D or Scan run calls `run()` from its worker thread when a stage move fails: the run is blocked in the call,
so it is paused for the whole ladder, and keeps everything it has written. Manual moves get it from the shell.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from openevos.acquisition.controller import InstrumentController

#: the objective counts as lowered at or below this Z (µm), as for the operator's Re-home (ui/shell.py)
Z_LOWERED_UM = 0.5
Z_TIMEOUT_S = 40.0
#: the drive part: two homings (16 s and 10 s on the unit, up to 120 s each), an approach, three slow legs, the return
JOB_TIMEOUT_S = 600.0

Log = Callable[[str, str], None]  # (level "info" | "warn" | "error", text)


@dataclass
class RecoveryOutcome:
    attempted: bool  # an attempt was claimed (the tripped axis' one attempt this session is used)
    recovered: bool  # the stage recovered and the objective is back: the caller may carry on
    message: str
    stage_ok: bool = False  # the stage recovered (the objective may still not be back)
    record: str = ""  # the evidence file, if the session has an evidence folder


class StageAutoRecovery:
    #: one recovery at a time in the process (the shell and a run may each hold an instance)
    _running = threading.Lock()

    def __init__(self, session: Any, *, log: Log | None = None, z_timeout_s: float = Z_TIMEOUT_S,
                 job_timeout_s: float = JOB_TIMEOUT_S):
        self.session = session
        self._log_fn = log
        self.z_timeout_s = z_timeout_s
        self.job_timeout_s = job_timeout_s

    # ------------------------------------------------------------------ queries (any thread)
    def service(self) -> Any:
        return next((s for s in getattr(self.session, "telemetry", None) or [] if hasattr(s, "claim_auto_recovery")),
                    None)

    def enabled(self) -> bool:
        return bool(getattr(self.session.settings.basic, "stage_auto_recovery", False))

    def blocker(self) -> str:
        """'' when an automatic recovery may start now; otherwise why not."""
        if not self.enabled():
            return "automatic stage recovery is switched off (Settings › Interface)"
        svc = self.service()
        if svc is None:
            return "no physical stage in this session"
        if self._running.locked():
            return "an automatic recovery is already running"
        why: str = svc.auto_recovery_blocker()
        return why

    def stage_locked(self) -> bool:
        svc = self.service()
        return bool(svc is not None and svc.monitor.latched)

    # ------------------------------------------------------------------ the ladder (blocking)
    def run(self, context: str, *, confirmed_z: bool = False) -> RecoveryOutcome:
        """Light off, objective down, the service's re-home + test + return, objective back. `context` names who
        asked (a run and its position, or a manual move); `confirmed_z`: the operator already confirmed working above
        the objective's safe height (a Multi-D run's confirmation), so raising it back is confirmed too."""
        why = self.blocker()
        if why:
            return RecoveryOutcome(False, False, why)
        if not self._running.acquire(blocking=False):
            return RecoveryOutcome(False, False, "an automatic recovery is already running")
        try:
            return self._run(context, confirmed_z)
        finally:
            self._running.release()

    def _run(self, context: str, confirmed_z: bool) -> RecoveryOutcome:
        svc = self.service()
        job = svc.claim_auto_recovery(f"automatic recovery ({context})")
        if isinstance(job, str):
            return RecoveryOutcome(False, False, job)
        self._log("warn", f"Stage: {job.rule} on {job.axis} ({job.trip_class}) during {context}. Recovering "
                          f"automatically (ADR-0067): light off, objective down, re-home X then Y, a slow test of "
                          f"{job.axis}, then back. {job.axis} gets one automatic attempt per session")
        ctl: InstrumentController = self.session.controller
        try:
            self._light_off(svc, job, ctl)
            z0 = ctl.focus_z()
        except Exception as exc:  # noqa: BLE001 - the claim must end either way (it blocks the operator's Re-home)
            return self._abandon(svc, job, f"unexpected error before the drives were touched ({exc!r})")
        if z0 is None:
            return self._abandon(svc, job, "the focus position is unknown, so the objective could not be lowered first")
        if z0 > Z_LOWERED_UM:
            try:
                InstrumentController.run(ctl.move_focus(0.0, timeout_s=self.z_timeout_s), timeout=self.z_timeout_s + 5)
            except Exception as exc:  # noqa: BLE001 - any failure: nothing goes to X or Y
                return self._abandon(svc, job, f"lowering the objective from Z {z0:.0f} µm to 0 failed ({exc})")
            svc.recovery_step(job, "objective lowered", from_um=round(z0, 1))
        else:
            svc.recovery_step(job, "objective already lowered", z_um=round(z0, 1))
        self._log("info", f"Stage recovery: re-homing X then Y, then testing {job.axis}")
        svc.run_auto_recovery(job)
        if not job.done.wait(self.job_timeout_s):
            msg = (f"the automatic recovery has not finished after {self.job_timeout_s:g} s (at: {job.stage}); the "
                   "stage stays locked until it ends")
            self._log("error", f"Stage recovery: {msg}")
            return RecoveryOutcome(True, False, msg)
        record = str(job.path or "")
        if not job.ok:
            self._log("error", f"Stage recovery: {job.message}")
            return RecoveryOutcome(True, False, job.message, record=record)
        if z0 > Z_LOWERED_UM:
            try:
                InstrumentController.run(ctl.move_focus(z0, timeout_s=self.z_timeout_s, confirmed=confirmed_z),
                                         timeout=self.z_timeout_s + 5)
            except Exception as exc:  # noqa: BLE001 - the stage is fine; the caller must not go on out of focus
                svc.recovery_step(job, "objective not raised back", to_um=round(z0, 1), why=str(exc))
                msg = f"{job.message}. The objective could not go back to Z {z0:.0f} µm ({exc})"
                self._log("warn", f"Stage recovery: {msg}")
                return RecoveryOutcome(True, False, msg, stage_ok=True, record=record)
            svc.recovery_step(job, "objective raised back", to_um=round(z0, 1))
        svc.recovery_step(job, "handed back", context=context)
        self._log("info", f"Stage recovery: {job.message}")
        return RecoveryOutcome(True, True, job.message, stage_ok=True, record=record)

    def _light_off(self, svc: Any, job: Any, ctl: InstrumentController) -> None:
        """Every LED off with the board's halt; if the controller still shows a lit channel, also an ordinary
        light-off command. Recorded either way: a light left on does not endanger the stage, so the ladder goes on."""
        lit = ctl.light.lit
        self.session.lights_off()
        still = ctl.light.lit
        if still is not None:
            try:
                InstrumentController.run(ctl.set_light(still[0] or "", 0.0, False), timeout=45.0)
                still = ctl.light.lit
            except Exception as exc:  # noqa: BLE001 - recorded; see above
                svc.recovery_step(job, "light off not confirmed", why=str(exc))
                return
        svc.recovery_step(job, "light off", was=f"{lit[0]} at {lit[1]:g} %" if lit else "off",
                          confirmed=still is None)

    def _abandon(self, svc: Any, job: Any, why: str) -> RecoveryOutcome:
        svc.abandon_auto_recovery(job, why)
        self._log("error", f"Stage recovery: {job.message}")
        return RecoveryOutcome(True, False, job.message, record=str(job.path or ""))

    def _log(self, level: str, text: str) -> None:
        if self._log_fn is not None:
            self._log_fn(level, text)
