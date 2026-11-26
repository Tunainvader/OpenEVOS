"""Runs a Multi-D plan (ADR-0065; kit docs 01 §6-§10, 04): timepoints → positions → one Z x C volume each, through
the existing ``ZStackRunner`` and the router's safety checks. Qt-free; runs on the task runner's worker thread.

States: ready → running ⇄ paused → completed | partial | failed.

* **Pause** takes effect at a safe boundary: between slices inside a volume (the Z-stack runner turns the light off
  while paused) or between volumes and timepoints. Resume continues with the next event not yet taken: nothing is
  taken twice. Before the next volume after a pause the focus read-back is checked again.
* **Stop & keep data** stops after the current move or exposure. The volume in progress is written as a partial
  volume, every volume already written stays, the manifest marks the rest "not acquired". There is no return move
  after a stop.
* **Writer failure or a full disk** stops dispatching at once; the run ends "partial" (images were written) or
  "failed" (none were), with the reason in the manifest. A frame is never dropped silently.
* **Timepoints.** Minimum start-to-start interval (default): a timepoint starts no earlier than the interval after
  the previous one *started*; if a cycle runs long the next starts as soon as it ends, marked late; a pause shifts the
  following start by the time paused. Fixed schedule: targets are offsets from the experiment start, kept through
  pauses; a missed target is skipped (``skip_missed``) or run once late (``run_late_once``). Never a catch-up burst.
* **Positions.** A position with coordinates is reached with a verified stage move; its anchor is the global or
  per-position focus reference. No autofocus is run: the kit gates it until it is qualified.
* **A stage drive trip** (ADR-0067). When a position move fails and the automatic stage recovery may run, the run
  pauses in it (light off, objective down, re-home, test, back), then retries that position's move once. If the retry
  fails with the stage unlocked the position is skipped and the run goes on; with the stage locked, or when no
  recovery could run, the run ends "partial" with everything written so far, as before.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum

from openevos.acquisition.capture import CaptureRejected
from openevos.acquisition.controller import CommandError
from openevos.acquisition.mda import (
    MDAPreflight,
    MultiDimPlan,
    PlanSummary,
    Schedule,
    VolumeJob,
    compile_jobs,
)
from openevos.acquisition.mda import Missed as MissedPolicy
from openevos.acquisition.scan_runner import RunControl, RunStopped
from openevos.imaging.mda_store import ExperimentWriter, StorageFull
from openevos.safety.restrictions import run_guard


class MDAState(str, Enum):
    READY = "ready"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"


@dataclass
class MDAProgress:
    """What the monitor shows; indices are zero-based (shown one-based)."""

    state: MDAState
    images_done: int
    images_total: int
    t_index: int = 0
    p_index: int = 0
    position_label: str = ""
    channel_label: str = ""
    z_index: int = 0
    z_um: float | None = None
    message: str = ""
    elapsed_s: float = 0.0
    timepoints: list[dict] = field(default_factory=list)


@dataclass
class MDAResult:
    state: MDAState
    folder: str | None
    message: str
    images_written: int
    timepoints: list[dict]
    seconds: float


ProgressFn = Callable[[MDAProgress], None]
ImageFn = Callable[[VolumeJob, str, int, int, object], None]  # job, slot, z index, images done in run, image


class SkipPosition(RuntimeError):
    """ADR-0067: after an automatic stage recovery the retried move failed with the stage unlocked: skip this
    position and go on."""


class MultiDimRunner:
    def __init__(self, session, writer: ExperimentWriter, *, control: RunControl | None = None,
                 monotonic: Callable[[], float] = time.monotonic, grace_s: float = 1.0, recovery=None):
        self.s = session
        self.cap = session.capture
        self.ctl = session.controller
        self.writer = writer
        self.control = control or RunControl()
        self.monotonic = monotonic
        self.grace_s = grace_s
        #: ADR-0067: a StageAutoRecovery (acquisition/stage_recovery.py), or None: a failed move ends the run
        self.recovery = recovery
        self.state = MDAState.READY
        self._paused_total = 0.0
        self._confirmed_above_safe = False

    # ------------------------------------------------------------------ public controls (any thread)
    def pause(self) -> None:
        self.control.pause()

    def resume(self) -> None:
        self.control.resume()

    def stop(self) -> None:
        self.control.stop()

    # ------------------------------------------------------------------ run
    def run(self, plan: MultiDimPlan, summary: PlanSummary, pre: MDAPreflight, *, confirmed_above_safe: bool = False,
            confirmed_positions: bool = False, progress: ProgressFn | None = None,
            on_image: ImageFn | None = None) -> MDAResult:
        """Inside the SafetyManager's run guard (ADR-0070): refused before anything moves when a restriction the
        operator confirmed covers what it needs."""
        caps = self.cap.run_capabilities(plan.camera, positions=len(plan.positions)) \
            if hasattr(self.cap, "run_capabilities") else set()
        with run_guard(self.s, "Multi-D", caps, self.control.stop):
            return self._run(plan, summary, pre, confirmed_above_safe=confirmed_above_safe,
                             confirmed_positions=confirmed_positions, progress=progress, on_image=on_image)

    def _run(self, plan: MultiDimPlan, summary: PlanSummary, pre: MDAPreflight, *, confirmed_above_safe: bool = False,
             confirmed_positions: bool = False, progress: ProgressFn | None = None,
             on_image: ImageFn | None = None) -> MDAResult:
        if not pre.ok:
            raise CaptureRejected("; ".join(pre.errors))
        if pre.confirm and not confirmed_above_safe:
            raise CaptureRejected("planes above the safe height need confirming first")
        if pre.confirm_positions and not confirmed_positions:
            raise CaptureRejected("moving between positions on the instrument needs the operator's confirmation first")
        t0 = self.monotonic()
        started = datetime.now(UTC)
        self._confirmed_above_safe = confirmed_above_safe
        self.writer.begin(plan, summary, started_at=started)
        for what, q in (("positions_on_instrument", pre.confirm_positions), ("above_safe_height", pre.confirm)):
            if q:
                self.writer.record("confirmed", what=what, question=q)
        self.state = MDAState.RUNNING
        total = summary.images
        done = 0
        prog = MDAProgress(MDAState.RUNNING, 0, total)
        tps: list[dict] = []
        status, message = MDAState.COMPLETED, ""
        vol_mb = plan.n_c * max(summary.n_z, 1) * summary.bytes_per_image / 1e6

        def emit(**kw) -> None:
            for k, v in kw.items():
                setattr(prog, k, v)
            prog.images_done, prog.elapsed_s, prog.timepoints = done, self.monotonic() - t0, tps
            prog.state = MDAState.PAUSED if self.control.paused and not self.control.stopped else self.state
            if progress:
                progress(prog)

        jobs = compile_jobs(plan)
        labels = {c.slot: c.label for c in plan.channels}
        current_t = -1
        tp_rec: dict = {}
        prev_start: float | None = None
        skipped = 0  # positions skipped after an automatic stage recovery (ADR-0067)
        try:
            for job in jobs:
                if job.t_index != current_t:  # a new timepoint: schedule it
                    if tp_rec:
                        tp_rec["finished_at"] = datetime.now(UTC).isoformat()
                        self.writer.timepoint(tp_rec)
                    current_t = job.t_index
                    skip, tp_rec = self._schedule(plan, job.t_index, t0, prev_start, emit)
                    tps.append(tp_rec)
                    if skip:
                        self.writer.timepoint(tp_rec)
                        tp_rec = {}
                        done += plan.n_p * plan.n_c * summary.n_z  # counted as handled (skipped), not written
                        self._skip_rest_of_timepoint(jobs, job, plan)
                        continue
                    prev_start = self.monotonic()
                self._checkpoint(emit)
                emit(t_index=job.t_index, p_index=job.p_index, position_label=job.position.label,
                     message=f"Timepoint {job.t_index + 1} of {plan.n_t}, {job.position.label}")
                try:
                    self._go_to(job, emit)
                except SkipPosition as skip:  # ADR-0067: recovered, but this position's retried move failed
                    self.writer.record("position_skipped", position_id=job.position.position_id,
                                       t_index=job.t_index, reason=str(skip))
                    skipped += 1
                    done += plan.n_c * summary.n_z  # handled (skipped), not written
                    emit(message=str(skip))
                    continue
                zplan = job.zstack_plan(plan, confirmed_above_safe=confirmed_above_safe)
                base = done

                def zprog(i: int, n: int, msg: str, _base=base) -> None:
                    if i < n:
                        emit(message=msg)

                def zimg(slot, zi, d, img, _job=job, _base=base) -> None:
                    nonlocal done
                    done = _base + d
                    emit(channel_label=labels.get(slot, slot), z_index=zi, z_um=_job.planned_z_um[zi])
                    if on_image:
                        on_image(_job, slot, zi, done, img)

                zs = self.cap.run_zstack(zplan, zprog, control=self.control, on_image=zimg)
                done = base + sum(len(st.images) for st in zs.stacks)
                if zs.stacks:
                    self.writer.commit_volume(plan, job, zs, need_mb=vol_mb)
                if zs.status == "stopped":
                    raise RunStopped(zs.message)
                if zs.status == "failed":
                    raise RuntimeError(f"{job.position.label}, timepoint {job.t_index + 1}: {zs.message}")
            if tp_rec:
                tp_rec["finished_at"] = datetime.now(UTC).isoformat()
                self.writer.timepoint(tp_rec)
            message = f"{self.writer.manifest['images_written']} images in {plan.n_t} timepoint" \
                      f"{'s' if plan.n_t != 1 else ''} at {plan.n_p} position{'s' if plan.n_p != 1 else ''}"
            if skipped:
                status = MDAState.PARTIAL
                message += f"; {skipped} position volume{'s' if skipped != 1 else ''} skipped after a stage recovery"
        except RunStopped as exc:
            status, message = MDAState.PARTIAL, f"stopped by the operator: {exc}"
        except StorageFull as exc:
            status, message = MDAState.PARTIAL, f"stopped safely: {exc}"
        except OSError as exc:
            status, message = MDAState.PARTIAL, f"writing failed, stopped: {exc}"
        except Exception as exc:  # noqa: BLE001 - every failure ends in a written manifest
            status, message = MDAState.PARTIAL, f"failed: {exc}"
        if status is MDAState.PARTIAL and self.writer.manifest.get("images_written", 0) == 0:
            status = MDAState.FAILED
        if tp_rec and "finished_at" not in tp_rec:
            tp_rec["finished_at"] = datetime.now(UTC).isoformat()
            tp_rec["status"] = "stopped"
            self.writer.timepoint(tp_rec)
        self.state = status
        self.writer.finish(status.value, message)
        emit(message=message)
        return MDAResult(status, str(self.writer.folder) if self.writer.folder else None, message,
                         self.writer.manifest.get("images_written", 0), tps, self.monotonic() - t0)

    # ------------------------------------------------------------------ pieces
    def _checkpoint(self, emit) -> None:
        """Safe boundary between volumes: wait while paused (no light: the Z-stack runner restored it), then check
        the focus read-back again before going on."""
        if self.control.paused:
            p0 = self.monotonic()
            self.writer.record("state", state="paused")
            emit(message="Paused between volumes; the light is off")
            self.control.checkpoint()
            self._paused_total += self.monotonic() - p0
            self.writer.record("state", state="running")
            if self.ctl.focus_z() is None:
                raise RuntimeError("focus read-back lost while paused: not resuming")
        self.control.checkpoint()

    def _go_to(self, job: VolumeJob, emit=None) -> None:
        pos = job.position
        if not pos.moves:
            return
        assert pos.x is not None and pos.y is not None
        here = self.ctl.stage_xy()
        if here is not None and abs(here[0] - pos.x) < 0.5 and abs(here[1] - pos.y) < 0.5:
            return
        try:
            self.ctl.run(self.ctl.move_stage(pos.x, pos.y))
        except CommandError as exc:
            self._recover_and_retry(job, exc, emit)
            return
        self.writer.record("move", position_id=pos.position_id, x=pos.x, y=pos.y, observed=self.ctl.stage_xy())

    def _recover_and_retry(self, job: VolumeJob, exc: CommandError, emit) -> None:
        """ADR-0067: the move to a position failed. With the automatic stage recovery allowed: pause in it, then retry
        the move once; a failed retry skips the position if the stage is unlocked (SkipPosition), else ends the run.
        Without it: the original error ends the run, as before."""
        pos = job.position
        rec = self.recovery
        why = rec.blocker() if rec is not None else "no automatic stage recovery in this run"
        if why:
            raise exc
        where = f"{pos.label}, timepoint {job.t_index + 1}"
        self.writer.record("state", state="paused", reason="automatic stage recovery (ADR-0067)", error=str(exc))
        if emit:
            emit(message=f"Stage drive tripped on the way to {pos.label}: recovering automatically (light off, "
                         "objective down, re-home, test); the run is paused")
        out = rec.run(f"Multi-D {where}", confirmed_z=self._confirmed_above_safe)
        self.writer.record("stage_recovery", position_id=pos.position_id, t_index=job.t_index,
                           recovered=out.recovered, message=out.message, record=out.record)
        if not out.recovered:
            raise RuntimeError(f"{where}: the stage drive tripped and the automatic recovery did not succeed: "
                               f"{out.message}") from exc
        self.writer.record("state", state="running")
        self.control.checkpoint()  # a Stop pressed during the recovery ends the run here
        try:
            self.ctl.run(self.ctl.move_stage(pos.x, pos.y))  # the one retry
        except CommandError as again:
            if rec.stage_locked():
                raise RuntimeError(f"{where}: the move failed again after the automatic recovery and the stage is "
                                   f"locked: {again}") from again
            raise SkipPosition(f"{where}: the move failed again after the automatic recovery ({again}); position "
                               "skipped") from again
        self.writer.record("move", position_id=pos.position_id, x=pos.x, y=pos.y, observed=self.ctl.stage_xy(),
                           after_recovery=True)

    def _wait_until(self, target: float, emit, msg: str, shift_on_pause: bool) -> float:
        """Sleep to `target` (monotonic), interruptibly; returns the (possibly shifted) target."""
        while True:
            now = self.monotonic()
            if self.control.paused:
                p0 = now
                emit(message="Paused while waiting for the next timepoint")
                self.control.checkpoint()
                dp = self.monotonic() - p0
                self._paused_total += dp
                if shift_on_pause:
                    target += dp
                continue
            if now >= target:
                return target
            emit(message=f"{msg}: next starts in {target - now:,.0f} s")
            self.control.sleep(min(1.0, target - now))

    def _schedule(self, plan: MultiDimPlan, t: int, t0: float, prev_start: float | None, emit) -> tuple[bool, dict]:
        """Wait for timepoint t; returns (skip it, its record)."""
        tm = plan.time
        rec: dict = {"t_index": t, "status": "run", "late_s": 0.0}
        if not tm.enabled or t == 0:
            rec["scheduled_offset_s"] = 0.0
            rec["started_at"] = datetime.now(UTC).isoformat()
            rec["started_offset_s"] = round(self.monotonic() - t0, 3)
            return False, rec
        if tm.schedule is Schedule.MIN_INTERVAL:
            assert prev_start is not None
            target = prev_start + tm.interval_s
            now = self.monotonic()
            if now > target + self.grace_s:
                rec["late_s"] = round(now - target, 3)
                rec["status"] = "late"
            else:
                target = self._wait_until(target, emit, f"Timepoint {t} done", shift_on_pause=True)
            rec["scheduled_offset_s"] = round(target - t0, 3)
        else:
            target = t0 + t * tm.interval_s
            rec["scheduled_offset_s"] = round(target - t0, 3)
            now = self.monotonic()
            if now > target + self.grace_s:
                next_target = t0 + (t + 1) * tm.interval_s
                if tm.missed is MissedPolicy.SKIP or (t + 1 < plan.n_t and now >= next_target):  # no burst
                    rec["status"] = "skipped"
                    rec["late_s"] = round(now - target, 3)
                    emit(message=f"Timepoint {t + 1} missed its time: skipped")
                    return True, rec
                rec["status"] = "late"
                rec["late_s"] = round(now - target, 3)
            else:
                self._wait_until(target, emit, f"Timepoint {t} done", shift_on_pause=False)
        rec["started_at"] = datetime.now(UTC).isoformat()
        rec["started_offset_s"] = round(self.monotonic() - t0, 3)
        return False, rec

    @staticmethod
    def _skip_rest_of_timepoint(jobs, first: VolumeJob, plan: MultiDimPlan) -> None:
        for _ in range(plan.n_p - 1 - first.p_index):
            next(jobs, None)
