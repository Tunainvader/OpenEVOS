"""The named-command port on the command core: parameters, units, actors, modes, confirmation,
interlocks, execution, STOP and audit (plan §6, §12.2, §13; ADR-0008, ADR-0010).

Ported from the retired `InstrumentGateway` tests. Each old guarantee is re-checked on the path
the application now uses: CoreGateway → CommandRouter → SafetyManager → HalDeviceBackend.
Check names follow the core: "limits" for capability ranges, "device_state" for activity/health,
"interlock:<name>" for cross-device rules.
"""

from __future__ import annotations

import dataclasses
import json
import threading
import time

import pytest

pytest.importorskip("numpy")  # the simulated instrument renders images with numpy

from openevos.backends.simulation import SimClock, SimulationConfig, build_simulated_instrument
from openevos.backends.simulation.motion import SimulatedXYStage
from openevos.core.application import build_core
from openevos.core.audit import AuditTrail
from openevos.core.clock import ThreadClock
from openevos.core.command_language import (
    DEFAULT_LIMITS,
    Actor,
    CommandAborted,
    CommandFailed,
    CommandRejected,
    CommandTimeout,
    ModeNotAvailable,
    OperationalMode,
    PolicyError,
    Quantity,
)
from openevos.core.core_gateway import CoreGateway
from openevos.core.device_manager import OwnershipRegistry
from openevos.core.instrument import InstrumentConfig, attach_registry, build_instrument
from openevos.hal.device_registry import DeviceRegistry, RegistryError
from openevos.hal.interfaces import DeviceError
from openevos.hal.status_model import RuntimeState
from openevos.hal.types import DeviceIdentity, DeviceStatus, Role
from openevos.inspector.evidence import EvidenceStage

START_XY = (55000.0, 37500.0)  # SimulationConfig default


def make(time_scale: float = 0.0, **opts):
    return build_instrument(InstrumentConfig(backend_options={"time_scale": time_scale, **opts}), initialize=False)


@pytest.fixture
def inst():
    i = make()
    yield i
    i.close()


@pytest.fixture
def gw(inst):
    return inst.gateway


@pytest.fixture
def slow():
    """Real-time motion (20 mm/s stage), for tests that need a move in progress."""
    i = make(time_scale=1.0)
    yield i
    i.close()


@pytest.fixture
def custom():
    """Build a gateway around a hand-made registry; closed after the test."""
    made = []

    def build(registry: DeviceRegistry, audit: AuditTrail | None = None) -> CoreGateway:
        core = build_core(ThreadClock(), ownership=OwnershipRegistry())
        made.append(core)
        attach_registry(core, registry, dict(DEFAULT_LIMITS))
        g = CoreGateway(core, registry, audit=audit)
        g.connect_all()
        return g

    yield build
    for core in made:
        core.shutdown()
        core.clock.stop()


def rejected(gw, command, check=None, **kw):
    with pytest.raises(CommandRejected) as ei:
        gw.execute(command, **kw)
    if check is not None:
        assert ei.value.check == check, ei.value
    return ei.value


def wait_idle(gw, role, timeout=5.0):
    deadline = time.monotonic() + timeout
    while gw.read(role).is_busy() and time.monotonic() < deadline:
        time.sleep(0.01)


# ------------------------------------------------------------------------------ registry/types
def test_registry_type_checks_roles():
    sim = build_simulated_instrument(SimulationConfig(time_scale=0.0))
    reg = DeviceRegistry()
    with pytest.raises(TypeError):
        reg.register(Role.XY_STAGE, sim.focus)
    reg.register(Role.XY_STAGE, sim.stage)
    with pytest.raises(RegistryError):
        reg.register(Role.XY_STAGE, sim.stage)
    with pytest.raises(RegistryError):
        reg.require(Role.FOCUS)


def test_effective_state_never_shows_unevidenced_real_device_as_ready():
    assert DeviceStatus(RuntimeState.READY, simulated=False).effective_state is RuntimeState.UNKNOWN
    assert DeviceStatus(RuntimeState.BUSY, simulated=False,
                        evidence_stage=EvidenceStage.IDENTIFIED).effective_state is RuntimeState.UNKNOWN
    assert DeviceStatus(RuntimeState.READY, simulated=False,
                        evidence_stage=EvidenceStage.RESPONSIVE).effective_state is RuntimeState.READY
    assert DeviceStatus(RuntimeState.READY, simulated=True).effective_state is RuntimeState.READY


def test_read_only_view_exposes_reads_but_no_setters(gw):
    view = gw.read(Role.XY_STAGE)
    assert view.get_position_um() == START_XY
    for name in ("move_absolute_um", "home", "stop", "connect", "halt"):
        with pytest.raises(AttributeError):
            getattr(view, name)
    with pytest.raises(AttributeError):
        view.anything = 1
    for role, name in ((Role.ILLUMINATION, "set_led"), (Role.INCUBATOR, "set_setpoint"),
                       (Role.CAMERA_MONO, "snap"), (Role.CAMERA_MONO, "set_exposure_ms")):
        assert not hasattr(gw.read(role), name)


# ------------------------------------------------------------------------------ parameters
def test_units_are_converted_to_canonical(gw):
    r = gw.execute("xy.move_absolute", x_um=Quantity(50, "mm"), y_um=(30.5, "mm"), wait=True)
    assert r.args == {"x_um": 50000.0, "y_um": 30500.0}
    assert gw.read(Role.XY_STAGE).get_position_um() == (50000.0, 30500.0)
    gw.execute("camera.set_exposure", camera="camera.mono", exposure_ms=Quantity(0.25, "s"))
    assert gw.read(Role.CAMERA_MONO).get_exposure_ms() == 250.0
    gw.execute("incubator.set_temperature", temperature_c=Quantity(310.15, "K"), confirmed=True)
    assert gw.read(Role.INCUBATOR).get_setpoints()["temperature"] == pytest.approx(37.0)


@pytest.mark.parametrize("kw", [
    {"x_um": Quantity(1, "ms"), "y_um": 1.0},  # wrong dimension
    {"x_um": Quantity(1, "furlong"), "y_um": 1.0},  # unknown unit
    {"x_um": float("nan"), "y_um": 1.0},
    {"x_um": float("inf"), "y_um": 1.0},
    {"x_um": True, "y_um": 1.0},  # bool is not a number
    {"x_um": "100", "y_um": 1.0},
    {"x_um": 1.0},  # missing y
    {"x_um": 1.0, "y_um": 1.0, "speed": 3},  # unknown parameter
])
def test_bad_parameters_rejected(gw, kw):
    rejected(gw, "xy.move_absolute", "params", **kw)
    assert gw.read(Role.XY_STAGE).get_position_um() == START_XY


def test_integer_positions_must_be_whole_and_known(gw):
    gw.execute("focus.move_absolute", z_um=1000.0, wait=True)
    rejected(gw, "objective.select", "params", position=2.5)
    rejected(gw, "objective.select", "limits", position=9)  # not in the capability record
    gw.execute("objective.select", position=2.0, wait=True)
    assert gw.read(Role.OBJECTIVE_TURRET).get_position() == 2


def test_bounds_from_capability_records(gw):
    err = rejected(gw, "xy.move_absolute", "limits", x_um=120000.0, y_um=1000.0)
    assert "axis x" in err.reason and "outside" in err.reason
    rejected(gw, "focus.move_absolute", "limits", z_um=-5.0)
    rejected(gw, "camera.set_exposure", "limits", camera="camera.mono", exposure_ms=10000.0)
    rejected(gw, "camera.set_gain", "limits", camera="camera.color", gain_db=20.0)
    rejected(gw, "led.set", "params", channel_id="TL", enabled=True, intensity_pct=101.0)  # static 0–100
    rejected(gw, "led.set", "limits", channel_id="UV9", enabled=True, intensity_pct=10.0)
    rejected(gw, "incubator.set_co2", "limits", co2_pct=25.0, confirmed=True)
    rejected(gw, "camera.snap", "kind", camera="xy")  # a snap does not apply to a stage


def test_relative_moves_are_resolved_from_observed_position_then_bounded(gw):
    r = gw.execute("xy.move_relative", dx_um=-1000.0, dy_um=500.0, wait=True)
    assert r.args == {"x_um": 54000.0, "y_um": 38000.0}
    rejected(gw, "xy.move_relative", "params", dx_um=60000.0, dy_um=0.0)  # larger than one step
    gw.execute("xy.move_absolute", x_um=500.0, y_um=500.0, wait=True)
    rejected(gw, "xy.move_relative", "limits", dx_um=-1000.0, dy_um=0.0)
    ev = gw.audit.events()[-1]
    assert ev.outcome == "rejected" and ev.reason.startswith("axis x")
    assert ev.params["resolved"] == {"x_um": -500.0, "y_um": 500.0}


# ------------------------------------------------------------------------------ actor and mode
@pytest.mark.parametrize("command,kw", [
    ("xy.move_relative", {"dx_um": 1.0, "dy_um": 0.0}),
    ("led.set", {"channel_id": "TL", "enabled": True, "intensity_pct": 10.0}),
    ("led.all_off", {}),
    ("camera.snap", {"camera": "camera.mono"}),
    ("incubator.set_temperature", {"temperature_c": 30.0, "confirmed": True}),
])
def test_ai_actor_never_commands_hardware(gw, command, kw):
    err = rejected(gw, command, "actor", actor=Actor.AI, **kw)
    assert "AI" in err.reason
    assert gw.audit.events()[-1].actor == "ai"


def test_ai_cannot_stop_or_clear_stop(gw):
    with pytest.raises(CommandRejected):
        gw.stop_all(actor=Actor.AI)
    assert not gw.stop_latched
    gw.stop_all()
    with pytest.raises(CommandRejected):
        gw.clear_stop(actor=Actor.AI)
    assert gw.stop_latched


def test_only_simulation_mode_accepts_state_changes(gw):
    for mode in (OperationalMode.INVENTORY, OperationalMode.CONNECTED_READ_ONLY):
        gw.policy.set_mode(mode)
        rejected(gw, "xy.move_relative", "mode", dx_um=1.0, dy_um=0.0)
    for mode in (OperationalMode.VALIDATED_CONTROL, OperationalMode.MAINTENANCE):
        with pytest.raises(ModeNotAvailable):
            gw.policy.set_mode(mode)
    gw.policy.set_mode(OperationalMode.SIMULATION)
    gw.execute("xy.move_relative", dx_um=1.0, dy_um=0.0, wait=True)


class NotSimulatedStage(SimulatedXYStage):
    """Pretends to be real hardware, to prove it can never be wired to the core this way."""

    def identify(self) -> DeviceIdentity:
        return dataclasses.replace(super().identify(), simulated=False, backend="pretend-real",
                                   display_name="Pretend-real stage")


def test_non_simulated_hal_device_is_refused_by_the_adapter(custom):
    reg = DeviceRegistry()
    reg.register(Role.XY_STAGE, NotSimulatedStage(SimClock(0.0), ((0, 1000), (0, 1000)), (500, 500), 1000.0))
    with pytest.raises(ValueError, match="not simulated"):
        custom(reg)


# ------------------------------------------------------------------------------ confirmation & interlocks
def test_confirmation_required(gw):
    rejected(gw, "incubator.set_temperature", "confirmation", temperature_c=30.0)
    rejected(gw, "incubator.set_enabled", "confirmation", control="co2", enabled=True)
    rejected(gw, "xy.home", "confirmation")
    gw.execute("incubator.set_temperature", temperature_c=30.0, confirmed=True)
    assert gw.audit.events()[-1].confirmed is True


def test_turret_turns_only_with_focus_at_safe_height(gw):
    err = rejected(gw, "objective.select", "interlock:instrument", position=5)
    assert "turret-safe height" in err.reason
    gw.execute("focus.move_absolute", z_um=1500.0, wait=True)
    gw.execute("objective.select", position=5, wait=True)
    assert gw.read(Role.OBJECTIVE_TURRET).current_objective().magnification == 40


def test_epi_led_needs_its_cube_and_carriage_needs_leds_off(gw):
    rejected(gw, "led.set", "interlock:instrument", channel_id="CUBE2", enabled=True, intensity_pct=50.0)
    gw.execute("carriage.select", position=2, wait=True)
    gw.execute("led.set", channel_id="CUBE2", enabled=True, intensity_pct=50.0)
    rejected(gw, "carriage.select", "interlock:instrument", position=3)
    gw.execute("led.set", channel_id="TL", enabled=True, intensity_pct=20.0)  # TL has no cube
    gw.execute("led.all_off")
    gw.execute("carriage.select", position=3, wait=True)


def test_busy_and_faulted_devices_reject_commands(slow):
    gw = slow.gateway
    gw.execute("xy.move_relative", dx_um=10000.0, dy_um=0.0)  # 0.5 s at 20 mm/s
    err = rejected(gw, "xy.move_relative", "device_state", dx_um=1.0, dy_um=0.0)
    assert "moving" in err.reason
    wait_idle(gw, Role.XY_STAGE)
    slow.registry.require(Role.FOCUS).inject_fault("encoder error (injected)")
    err = rejected(gw, "focus.move_relative", "device_state", dz_um=1.0)
    assert "fault" in err.reason


def test_unhomed_axis_rejects_moves():
    i = make(start_homed=False)
    try:
        gw = i.gateway
        rejected(gw, "xy.move_relative", "interlock:homed", dx_um=1.0, dy_um=0.0)
        gw.execute("xy.home", confirmed=True, wait=True)
        gw.execute("xy.move_relative", dx_um=1.0, dy_um=0.0, wait=True)
    finally:
        i.close()


# ------------------------------------------------------------------------------ execution
def test_device_error_becomes_command_failed(gw, inst, monkeypatch):
    def boom(*_a):
        raise DeviceError("driver fault (injected)")
    monkeypatch.setattr(inst.registry.require(Role.XY_STAGE), "move_absolute_um", boom)
    with pytest.raises(CommandFailed, match="driver fault"):
        gw.execute("xy.move_relative", dx_um=1.0, dy_um=0.0)
    ev = gw.audit.events()[-1]
    assert (ev.decision, ev.outcome) == ("allowed", "failed")


class StuckStage(SimulatedXYStage):
    """Starts moves but never finishes them (stop requests are counted, and ignored)."""

    stopped = 0
    stuck = False

    def move_absolute_um(self, x_um: float, y_um: float) -> None:
        self.stuck = True

    def is_busy(self) -> bool:
        return self.stuck

    def get_status(self) -> DeviceStatus:
        return dataclasses.replace(super().get_status(), state=RuntimeState.READY)

    def stop(self) -> None:
        StuckStage.stopped += 1


def _stuck(custom, timeout_s: float) -> CoreGateway:
    reg = DeviceRegistry()
    reg.register(Role.XY_STAGE, StuckStage(SimClock(0.0), ((0, 1e5), (0, 1e5)), (0, 0), 1e4))
    g = custom(reg)
    g.catalog["xy.move_absolute"] = dataclasses.replace(g.catalog["xy.move_absolute"], timeout_s=timeout_s)
    StuckStage.stopped = 0
    return g


def test_timeout_stops_device_requires_recovery_and_is_audited(custom):
    g = _stuck(custom, 0.2)
    with pytest.raises(CommandTimeout):
        g.execute("xy.move_absolute", x_um=10.0, y_um=10.0, wait=True)
    assert StuckStage.stopped >= 1
    assert g.audit.events()[-1].outcome == "timeout"
    assert g.core.store.get("xy").recovery_required
    err = rejected(g, "xy.move_absolute", x_um=20.0, y_um=20.0)
    assert "requires recovery" in err.reason  # (a stuck stage also still reports "moving")


def test_background_move_timeout_is_stopped_and_audited(custom):
    g = _stuck(custom, 0.2)
    r = g.execute("xy.move_absolute", x_um=10.0, y_um=10.0)
    assert r.completed is False
    deadline = time.monotonic() + 3.0
    while not any(e.outcome == "timeout" for e in g.audit.events()) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert StuckStage.stopped >= 1
    assert any(e.outcome == "timeout" and e.command == "xy.move_absolute" for e in g.audit.events())


# ------------------------------------------------------------------------------ STOP
def test_stop_halts_motion_switches_leds_off_and_latches(slow):
    gw = slow.gateway
    gw.execute("led.set", channel_id="TL", enabled=True, intensity_pct=30.0)
    gw.execute("xy.move_absolute", x_um=75000.0, y_um=37500.0)  # 1 s move
    time.sleep(0.4)
    report = gw.stop_all(reason="test")
    assert set(report.halted) == {r.value for r in Role}
    x, _ = gw.read(Role.XY_STAGE).get_position_um()
    assert START_XY[0] < x < 75000 and not gw.read(Role.XY_STAGE).is_busy()
    time.sleep(0.3)
    assert gw.read(Role.XY_STAGE).get_position_um()[0] == x  # stays where it stopped
    assert not any(s.enabled for s in gw.read(Role.ILLUMINATION).get_led_states())
    err = rejected(gw, "xy.move_relative", "stop_latch", dx_um=1.0, dy_um=0.0)
    assert "STOP is engaged" in err.reason
    gw.execute("led.all_off")  # safe-direction commands remain available
    gw.execute("camera.stop_live", camera="camera.mono")
    gw.clear_stop()
    gw.execute("xy.move_relative", dx_um=1.0, dy_um=0.0, wait=True)
    cmds = [e.command for e in gw.audit.events()]
    assert "system.stop_all" in cmds and "system.clear_stop" in cmds


def test_stop_does_not_wait_for_a_device_call_in_flight(slow, monkeypatch):
    """A command stuck inside a device call must not delay STOP, and must not move afterwards."""
    gw = slow.gateway
    stage = slow.registry.require(Role.XY_STAGE)
    entered, release = threading.Event(), threading.Event()
    real_move = stage.move_absolute_um

    def slow_driver(x, y):
        entered.set()
        release.wait(5)
        real_move(x, y)

    monkeypatch.setattr(stage, "move_absolute_um", slow_driver)
    errors: list = []
    t = threading.Thread(target=lambda: _capture(errors, gw.execute, "xy.move_absolute",
                                                 x_um=75000.0, y_um=37500.0), daemon=True)
    t.start()
    assert entered.wait(2)
    t0 = time.monotonic()
    gw.stop_all()
    assert time.monotonic() - t0 < 0.5 and gw.stop_latched
    release.set()
    t.join(5)
    time.sleep(0.2)
    assert not stage.is_busy()  # the in-flight move was halted, not carried out
    assert gw.read(Role.XY_STAGE).get_position_um()[0] < 60000
    assert errors and isinstance(errors[0], CommandAborted)


def _capture(errors, fn, *a, **kw):
    try:
        fn(*a, **kw)
    except Exception as exc:  # noqa: BLE001
        errors.append(exc)


def test_stop_aborts_a_waiting_command(slow):
    gw = slow.gateway
    errors: list = []
    t = threading.Thread(target=lambda: _capture(errors, gw.execute, "xy.move_absolute",
                                                 x_um=100000.0, y_um=37500.0, wait=True), daemon=True)
    t.start()
    time.sleep(0.3)
    gw.stop_all(reason="mid-move")
    t.join(5)
    assert errors and isinstance(errors[0], CommandAborted)
    assert [e.outcome for e in gw.audit.events() if e.command == "xy.move_absolute"] == ["aborted"]
    assert not gw.read(Role.XY_STAGE).is_busy()


def test_incubator_keeps_running_through_stop(gw):
    """Owner decision 2026-11-12, ADR-0006: STOP must not cut incubator heating or gas."""
    gw.execute("incubator.set_enabled", control="temperature", enabled=True, confirmed=True)
    gw.stop_all()
    assert gw.read(Role.INCUBATOR).get_enabled()["temperature"] is True


def test_exclusive_owner(gw):
    with gw.exclusive("acquisition:test"):
        rejected(gw, "xy.move_relative", "exclusive", dx_um=1.0, dy_um=0.0)
        gw.execute("xy.move_relative", dx_um=1.0, dy_um=0.0, owner="acquisition:test",
                   actor=Actor.ACQUISITION, wait=True)
        gw.execute("led.all_off")  # safe-direction still allowed
        with pytest.raises(PolicyError), gw.exclusive("someone-else"):
            pass
        assert gw.policy.exclusive_owner == "acquisition:test"
    assert gw.policy.exclusive_owner is None
    gw.execute("xy.move_relative", dx_um=1.0, dy_um=0.0, wait=True)


# ------------------------------------------------------------------------------ audit
def test_every_request_is_audited_with_plan_fields(gw):
    n0 = len(gw.audit)
    gw.execute("xy.move_relative", dx_um=5.0, dy_um=0.0, wait=True)
    with pytest.raises(CommandRejected):
        gw.execute("xy.move_relative", dx_um=1e9, dy_um=0.0)
    with pytest.raises(CommandRejected):
        gw.execute("no.such.command")
    events = gw.audit.events()[n0:]
    assert [e.outcome for e in events] == ["ok", "rejected", "rejected"]
    ok = events[0]
    assert ok.device == "XY stage (simulated)" and ok.backend == "simulation" and ok.simulated
    assert ok.mode == "simulation" and ok.actor == "operator" and ok.risk == "motion"
    assert ok.params["dx_um"] == 5.0 and ok.params["resolved"]["x_um"] == START_XY[0] + 5.0
    assert ok.checks[-1] == "core" and ok.state_changing
    assert ok.software_version and ok.timestamp.endswith("Z")
    assert events[2].reason == "unknown command"


def test_audit_trail_appends_jsonl(tmp_path, custom):
    path = tmp_path / "audit" / "audit.jsonl"
    sim = build_simulated_instrument(SimulationConfig(time_scale=0.0))
    g = custom(sim.registry, audit=AuditTrail(path))
    g.execute("led.set", channel_id="TL", enabled=True, intensity_pct=12.0)
    lines = [json.loads(x) for x in path.read_text("utf-8").splitlines()]
    assert len(lines) == len(g.audit) == 9  # 8 connects + 1 command
    assert lines[-1]["command"] == "led.set" and lines[-1]["params"]["intensity_pct"] == 12.0


def test_catalog_describes_every_command_and_maps_to_the_core(gw):
    from openevos.core.models import COMMAND_DEVICE_KIND

    for spec in gw.command_specs():
        assert spec.timeout_s > 0 and spec.summary and spec.abort_behavior
        assert spec.core_kind in COMMAND_DEVICE_KIND or (spec.safe_stop and not spec.core_kind), spec.name
        for p in spec.params:
            if p.kind is float:
                assert p.unit, f"{spec.name}.{p.name} has no unit"
                assert p.name.endswith(("_um", "_ms", "_db", "_pct", "_c")), p.name
        if spec.risk.value in ("motion", "environment") and not spec.safe_stop:
            assert spec.readback, spec.name
