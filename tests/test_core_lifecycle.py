"""Handoff §23.4: command lifecycle (deterministic: ManualWorker + ManualClock)."""

from core_helpers import ScriptedBackend, stage_move

from openevos.core.event_bus import HardwareFaultEvent
from openevos.core.models import Activity, Connection, Health
from openevos.core.models import CommandStatus as S
from openevos.safety.interlocks import resource

STAGE = "sim-stage"


def _run(core):
    core.devices.worker(STAGE).run_pending()


def test_success_path_and_strict_ordering(manual_core):
    core, _sim, clock, rec = manual_core
    t = core.router.submit(stage_move(1000, 500))
    assert t.status is S.ACCEPTED
    assert core.store.get(STAGE).requested_position.as_dict() == {"x": 1000.0, "y": 500.0}
    assert core.store.get(STAGE).activity is Activity.IDLE  # nothing has started yet
    _run(core)
    assert t.status is S.RUNNING and core.store.get(STAGE).activity is Activity.MOVING
    clock.advance(0.1)
    assert t.status is S.RUNNING  # travel takes 0.5 s at 2000 um/s
    clock.advance(1.0)
    assert t.status is S.SUCCEEDED and "verified by readback" in t.record.message
    assert t.history == (S.QUEUED, S.VALIDATING, S.ACCEPTED, S.RUNNING, S.SUCCEEDED)
    assert rec.statuses(t.command_id) == ["queued", "validating", "accepted", "running", "succeeded"]
    snap = core.store.get(STAGE)
    assert snap.observed_position.as_dict() == {"x": 1000.0, "y": 500.0} and snap.requested_position is None
    assert core.resources.holders() == {} and clock.pending() == 0  # timeout timer cancelled


def test_rejected_commands_never_reach_the_backend(manual_core):
    core, sim, _clock, _rec = manual_core
    calls = []
    orig = sim.stage.start
    sim.stage.start = lambda c, x: (calls.append(c), orig(c, x))
    t = core.router.submit(stage_move(10**6, 0))
    _run(core)
    assert t.status is S.REJECTED and "outside" in t.record.message and calls == []
    assert core.store.get(STAGE).requested_position is None


def test_asynchronous_failure_reaches_failed_and_blocks_next_command(manual_core):
    core, sim, clock, rec = manual_core
    sim.stage.fail_next_move = "simulated motor stall"
    t = core.router.submit(stage_move(1000, 0))
    _run(core)
    clock.advance(5)
    assert t.status is S.FAILED and "motor stall" in t.record.message
    assert any(e.error_code == "SIM_FAULT" for e in rec.of(HardwareFaultEvent))
    assert core.resources.holders() == {}
    assert core.store.get(STAGE).health is Health.FAULT
    nxt = core.router.submit(stage_move(0, 0))
    assert nxt.status is S.REJECTED and "health is fault" in nxt.record.message


def test_backend_start_exception_is_failed(manual_core):
    core, sim, _clock, _rec = manual_core
    sim.stage.start_raises = "driver exploded"
    t = core.router.submit(stage_move(10, 10))
    _run(core)
    assert t.status is S.FAILED and "driver exploded" in t.record.message and core.resources.holders() == {}


def test_cancel_before_start(manual_core):
    core, sim, _clock, _rec = manual_core
    calls = []
    orig = sim.stage.start
    sim.stage.start = lambda c, x: (calls.append(c), orig(c, x))
    t = core.router.submit(stage_move(1000, 0))
    assert t.cancel()
    _run(core)
    assert t.status is S.CANCELLED and calls == [] and core.resources.holders() == {}
    assert not t.cancel()  # terminal


def test_cancel_while_running_stops_midway(manual_core):
    core, _sim, clock, _rec = manual_core
    t = core.router.submit(stage_move(2000, 0))  # 1.0 s of travel
    _run(core)
    clock.advance(0.5)
    assert t.cancel()
    assert t.status is S.CANCELLED and S.CANCELLING in t.history
    x = core.store.get(STAGE).observed_position.get("x")
    assert 0 < x < 2000 and core.store.get(STAGE).activity is Activity.IDLE
    assert core.resources.holders() == {}
    clock.advance(5)
    assert t.status is S.CANCELLED  # the original arrival timer never fires


def test_timeout_keeps_reservation_until_recovery_and_ignores_late_events(manual_core):
    core, sim, clock, rec = manual_core
    sim.stage.stall_next_move = True
    t = core.router.submit(stage_move(1000, 0, timeout_s=2.0))
    _run(core)
    clock.advance(2.5)
    assert t.status is S.TIMED_OUT
    snap = core.store.get(STAGE)
    assert snap.recovery_required and resource(STAGE) in core.resources.holders()[t.command_id].exclusive
    assert any(e.error_code == "TIMEOUT" for e in rec.of(HardwareFaultEvent))
    blocked = core.router.submit(stage_move(0, 0))
    assert blocked.status is S.REJECTED and "requires recovery" in blocked.record.message
    core.router.recover_device(STAGE)
    assert not core.store.get(STAGE).recovery_required and core.resources.holders() == {}
    ok = core.router.submit(stage_move(0, 0))
    assert ok.status is S.ACCEPTED


def test_readback_mismatch_fails_and_requires_recovery(manual_core):
    core, sim, clock, rec = manual_core
    sim.stage.readback_offset = 5.0  # tolerance is 0.5 um
    t = core.router.submit(stage_move(100, 100))
    _run(core)
    clock.advance(1)
    assert t.status is S.FAILED and "verification failed" in t.record.message
    assert core.store.get(STAGE).recovery_required
    assert any(e.error_code == "READBACK_MISMATCH" for e in rec.of(HardwareFaultEvent))


def test_unexpected_disconnect_during_motion(manual_core):
    core, sim, clock, rec = manual_core
    t = core.router.submit(stage_move(2000, 0))
    _run(core)
    clock.advance(0.2)
    sim.stage.simulate_unexpected_disconnect()
    snap = core.store.get(STAGE)
    assert t.status is S.FAILED and snap.connection is Connection.DISCONNECTED and snap.health is Health.FAULT
    assert any(e.error_code == "DEVICE_FAULT" for e in rec.of(HardwareFaultEvent))


def test_protocol_violations_and_stale_callbacks(manual_core):
    core, sim, _clock, _rec = manual_core
    be = ScriptedBackend()
    core.devices.register(be, sim.stage.capabilities({"x": (-10, 10), "y": (-10, 10)}).model_copy(update={"device_id": "scripted"}))  # noqa: E501
    core.devices.connect("scripted")

    def submit(**kw):
        t = core.router.submit(stage_move(0, 0, **kw).model_copy(update={"device_id": "scripted"}))
        core.devices.worker("scripted").run_pending()
        return t

    # completion without a confirmed start is a backend bug -> FAILED
    t1 = submit()
    be.ctx.complete(be.position)
    assert t1.status is S.FAILED and "without confirming start" in t1.record.message

    # duplicate start and duplicate/late completion are ignored, state stays correct
    t2 = submit()
    be.ctx.mark_started()
    stale0 = core.router.stale_backend_events
    be.ctx.mark_started()
    be.ctx.complete(be.position)
    be.ctx.complete(be.position)
    be.ctx.fail("late failure")
    assert t2.status is S.SUCCEEDED and core.router.stale_backend_events == stale0 + 3
    assert t2.history.count(S.SUCCEEDED) == 1


def test_resources_are_released_before_terminal_event(manual_core):
    """A subscriber may chain the next command from the SUCCEEDED callback."""
    core, _sim, clock, _rec = manual_core
    from openevos.core.event_bus import CommandLifecycleEvent

    chained = []

    def on_event(e):
        if e.status is S.SUCCEEDED and not chained:
            chained.append(core.router.submit(stage_move(0, 0)))

    core.bus.subscribe(CommandLifecycleEvent, on_event)
    core.router.submit(stage_move(100, 0))
    _run(core)
    clock.advance(1)
    assert chained and chained[0].status is S.ACCEPTED
