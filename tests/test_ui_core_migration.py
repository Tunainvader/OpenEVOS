"""Acceptance tests for the UI migration onto the command core (ADR-0007/0008/0009, ADR-0006).

The desktop's controller path is Instrument.gateway (CoreGateway) -> core.router -> SafetyManager
-> resources -> DeviceWorker -> HalDeviceBackend -> simulated device. These tests drive that path
the way the UI does, and check the core's guarantees hold for it.
"""

from __future__ import annotations

import pytest

pytest.importorskip("numpy")

from openevos.core.command_language import CommandRejected
from openevos.core.core_gateway import CoreGateway
from openevos.core.event_bus import CommandLifecycleEvent
from openevos.core.instrument import InstrumentConfig, build_instrument
from openevos.core.models import (
    Activity,
    AllIlluminationOff,
    BackendKind,
    CommandOrigin,
    CommandStatus,
    Connection,
    DeviceKind,
    FirmwareMode,
    Health,
    MoveStageAbsolute,
    SetIllumination,
)
from openevos.hal.types import Role
from openevos.safety.capabilities import (
    AxisCapability,
    AxisLimits,
    CapabilitySource,
    DeviceCapabilities,
)

S = CommandStatus


@pytest.fixture
def inst():
    i = build_instrument(InstrumentConfig(backend_options={"time_scale": 0.0}))
    yield i
    i.close()


class PhysicalProbe:
    """A stand-in physical backend that records whether anything ever reached it."""

    backend_kind = BackendKind.PHYSICAL

    def __init__(self, device_id: str, kind: DeviceKind, firmware: FirmwareMode):
        self.device_id, self.kind, self.firmware = device_id, kind, firmware
        self.physical_id = f"probe:{device_id}"
        self.started: list = []
        self._report = None
        self._seq = 0

    def attach(self, reporter):
        self._report = reporter

    def connect(self):
        self._seq += 1
        self._report(seq=self._seq, connection=Connection.CONNECTED, health=Health.NORMAL,
                     activity=Activity.IDLE, firmware_mode=self.firmware)

    def disconnect(self):
        pass

    def start(self, command, ctx):
        self.started.append(command)

    def cancel(self, command_id):
        pass

    def read_position(self):
        return None

    def recover(self):
        pass


# --------------------------------------------------------------------------------------------
def test_ui_path_runs_on_the_command_core(inst):
    assert isinstance(inst.gateway, CoreGateway)
    assert set(inst.core.devices.device_ids()) == {r.value for r in inst.registry.roles()}
    snaps = inst.core.store.snapshot_all()
    assert all(s.connection is Connection.CONNECTED for s in snaps.values())
    assert inst.audit.events()[0].params["execution"] == "command core"


def test_ui_stage_move_full_lifecycle(inst):
    events = []
    inst.core.bus.subscribe(CommandLifecycleEvent, events.append)
    x0, y0 = inst.controller.xy_um()
    inst.gateway.execute("xy.move_absolute", x_um=x0 + 250.0, y_um=y0, wait=True)
    ids = {e.command_id for e in events}
    assert len(ids) == 1
    seen = [e.status for e in events]
    assert seen == [S.QUEUED, S.VALIDATING, S.ACCEPTED, S.RUNNING, S.SUCCEEDED]
    snap = inst.core.store.get(Role.XY_STAGE.value)
    assert snap.observed_position.get("x") == pytest.approx(x0 + 250.0)
    assert snap.activity is Activity.IDLE and snap.requested_position is None
    assert inst.core.resources.holders() == {}


def test_ui_rejects_candidate_limits_on_non_simulated_stage(inst):
    probe = PhysicalProbe("probe-stage", DeviceKind.XY_STAGE, FirmwareMode.APPLICATION)
    caps = DeviceCapabilities(
        device_id="probe-stage", backend_kind=BackendKind.PHYSICAL, source=CapabilitySource.CANDIDATE,
        supported_commands=frozenset({"stage.move_absolute"}),
        axes=tuple(AxisCapability(name=a, limits=AxisLimits(minimum=0, maximum=100_000, unit="um"),
                                  coordinate_frame="stage", calibration_valid=True, readback_tolerance=0.5)
                   for a in ("x", "y")))
    inst.core.devices.register(probe, caps)
    inst.core.devices.connect("probe-stage")
    t = inst.core.router.submit(MoveStageAbsolute(device_id="probe-stage", origin=CommandOrigin.UI,
                                                  x=10, y=10, unit="um", frame="stage"))
    assert t.status is S.REJECTED and t.record.check == "provenance"
    assert "candidate" in t.record.message and "not verified" in t.record.message
    assert probe.started == []


def test_ui_quarantines_bootloader_led_board(inst):
    probe = PhysicalProbe("probe-led", DeviceKind.ILLUMINATION, FirmwareMode.BOOTLOADER)
    caps = DeviceCapabilities(device_id="probe-led", backend_kind=BackendKind.PHYSICAL,
                              source=CapabilitySource.VERIFIED, evidence_ids=("EV-test",),
                              supported_commands=frozenset({"illumination.set", "illumination.all_off"}),
                              illumination_channels=("TL",))
    inst.core.devices.register(probe, caps)
    inst.core.devices.connect("probe-led")
    on = inst.core.router.submit(SetIllumination(device_id="probe-led", origin=CommandOrigin.UI,
                                                 channel="TL", intensity_percent=10, enabled=True))
    off = inst.core.router.submit(AllIlluminationOff(device_id="probe-led", origin=CommandOrigin.UI))
    for t in (on, off):  # even "safe-direction" commands never reach a bootloader
        assert t.status is S.REJECTED and "bootloader" in t.record.message.lower()
    assert probe.started == []
    assert inst.core.store.get("probe-led").firmware_mode is FirmwareMode.BOOTLOADER


def test_ui_timeout_recovery_lock_workflow():
    inst = build_instrument(InstrumentConfig(backend_options={"time_scale": 1.0}), initialize=False)
    try:
        gw, core = inst.gateway, inst.core
        stage = inst.registry.require(Role.XY_STAGE)
        stage._speed = 1.0  # µm/s: the move below would take minutes
        x0, y0 = inst.controller.xy_um()
        t = core.router.submit(MoveStageAbsolute(device_id="xy", origin=CommandOrigin.UI, x=x0 + 500, y=y0,
                                                 unit="um", frame="stage", timeout_s=0.3))
        assert t.wait(5.0) is S.TIMED_OUT
        snap = core.store.get("xy")
        assert snap.recovery_required
        assert core.resources.owners_of("device:xy") == {t.command_id}  # reservation kept until recovery
        with pytest.raises(CommandRejected) as exc:
            gw.execute("xy.move_relative", dx_um=1.0, dy_um=0.0)
        assert exc.value.check == "recovery"
        gw.recover(Role.XY_STAGE)
        assert not core.store.get("xy").recovery_required
        assert core.resources.owners_of("device:xy") == set()
        assert not stage.is_busy()  # recovery halted the stalled move
        stage._speed = 1e6
        gw.execute("xy.move_relative", dx_um=1.0, dy_um=0.0, wait=True)
    finally:
        inst.close()


def test_ui_stop_preserves_incubator_state(inst):
    gw, ctl = inst.gateway, inst.controller
    ctl.incubator_set("temperature", 36.5, confirmed=True)
    inc = gw.read(Role.INCUBATOR)
    before = (inc.get_setpoints(), inc.get_enabled())
    snap_before = inst.core.store.get(Role.INCUBATOR.value)
    report = ctl.stop("test STOP")
    assert gw.stop_latched and "illumination" in report.halted
    assert (inc.get_setpoints(), inc.get_enabled()) == before
    snap_after = inst.core.store.get(Role.INCUBATOR.value)
    assert snap_after.connection is Connection.CONNECTED and snap_after.health is snap_before.health
    assert not snap_after.recovery_required
    with pytest.raises(CommandRejected) as exc:  # STOP latches every new device command
        ctl.incubator_set("temperature", 37.0, confirmed=True)
    assert exc.value.check == "stop_latch"


def test_incubator_change_needs_confirmation(inst):
    with pytest.raises(CommandRejected) as exc:
        inst.controller.incubator_set("temperature", 36.0, confirmed=False)
    assert exc.value.check == "confirmation"


# ------------------------------------------------------------------------- interlocks via core
def test_turret_refused_while_focus_above_safe_height(inst):
    gw = inst.gateway
    gw.execute("focus.move_absolute", z_um=gw.policy.limits["turret_safe_z_um"] + 200, wait=True)
    with pytest.raises(CommandRejected) as exc:
        gw.execute("objective.select", position=1, wait=True)
    assert exc.value.check == "interlock:instrument" and "turret-safe" in exc.value.reason


def test_carriage_refused_while_epi_led_on(inst):
    ctl, gw = inst.controller, inst.gateway
    ctl.select_channel("GFP").result(timeout=10)
    with pytest.raises(CommandRejected) as exc:
        gw.execute("carriage.select", position=1, wait=True)
    assert exc.value.check == "interlock:instrument" and "epi LED" in exc.value.reason


def test_epi_led_refused_when_its_cube_is_not_in_the_path(inst):
    gw = inst.gateway
    gw.execute("led.all_off")
    gw.execute("carriage.select", position=1, wait=True)
    illum = gw.read(Role.ILLUMINATION)
    other = next(c for c in illum.get_channels() if c.carriage_slot not in (None, 1))
    with pytest.raises(CommandRejected) as exc:
        gw.execute("led.set", channel_id=other.channel_id, enabled=True, intensity_pct=10.0)
    assert exc.value.check == "interlock:instrument"


def test_snap_refused_while_live_and_light_off_always_allowed(inst):
    ctl, gw = inst.controller, inst.gateway
    ctl.set_live(True)
    cam = ctl.active_camera.value
    with pytest.raises(CommandRejected) as exc:
        gw.execute("camera.snap", camera=cam)
    # the camera is ACQUIRING, so both the activity rule and the not-live interlock refuse it
    assert exc.value.check == "device_state" and "live" in exc.value.reason
    ctl.stop()
    gw.execute("led.all_off")  # safe-direction: admitted while STOP is latched
    gw.execute("camera.stop_live", camera=cam)


def test_exposure_outside_camera_range_is_refused_by_core(inst):
    with pytest.raises(CommandRejected) as exc:
        inst.gateway.execute("camera.set_exposure", camera=inst.controller.active_camera.value, exposure_ms=59_999.0)
    assert exc.value.check == "limits"


def test_firmware_operation_refused_before_core(inst):
    with pytest.raises(CommandRejected) as exc:
        inst.gateway.execute("led.flash_firmware")
    assert exc.value.check == "firmware"
