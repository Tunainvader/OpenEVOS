"""Controller and acquisition engine against the simulated instrument (time_scale 0)."""

from __future__ import annotations

import json

import pytest

np = pytest.importorskip("numpy")

from openevos.core.acquisition import (
    AcquisitionPlan,
    ChannelStep,
    StagePosition,
    TimeLapse,
    ZStack,
)
from openevos.core.command_language import CommandRejected
from openevos.core.instrument import (
    BackendUnavailable,
    InstrumentConfig,
    build_instrument,
)
from openevos.hal.types import Role


@pytest.fixture
def inst():
    i = build_instrument(InstrumentConfig(backend_options={"time_scale": 0.0}))
    yield i
    i.close()


def leds_on(gw) -> list[str]:
    return [s.channel_id for s in gw.read(Role.ILLUMINATION).get_led_states() if s.enabled]


# ------------------------------------------------------------------------------ device manager
def test_backend_selection_is_configuration_driven_and_audited(inst):
    first = inst.audit.events()[0]
    assert first.command == "backend.selected" and first.params["backend"] == "simulation"
    assert inst.gateway.mode.value == "simulation"
    with pytest.raises(BackendUnavailable):
        build_instrument(InstrumentConfig(backend="micromanager"))


def test_channel_presets_derive_from_devices(inst):
    names = [p.name for p in inst.controller.presets]
    assert names == ["Brightfield", "Phase contrast", "Brightfield (mono)", "DAPI", "GFP", "RFP", "Cy5"]
    gfp = inst.controller.preset("GFP")
    assert gfp.camera is Role.CAMERA_MONO and gfp.carriage_position == 2 and gfp.display_rgb[1] == 255


# ------------------------------------------------------------------------------ controller
def test_channel_switch_sequences_carriage_phase_and_leds(inst):
    ctl, gw = inst.controller, inst.gateway
    assert ctl.active_preset.name == "Phase contrast"
    ph = gw.read(Role.PHASE_TURRET)
    assert next(p.label for p in ph.get_positions() if p.index == ph.get_position()) == "Ph1"
    ctl.select_channel("RFP").result(timeout=10)
    assert gw.read(Role.ILLUMINATION).get_carriage_position() == 3
    assert leds_on(gw) == ["CUBE3"] and ctl.active_camera is Role.CAMERA_MONO
    ctl.select_channel("Brightfield").result(timeout=10)
    assert leds_on(gw) == ["TL"] and gw.read(Role.ILLUMINATION).get_carriage_position() == 5
    assert ph.get_position() == 1  # Open


def test_objective_change_lowers_focus_and_applies_parfocal_offset(inst):
    ctl = inst.controller
    z10 = ctl.z_um()
    ctl.select_objective(5).result(timeout=10)
    assert ctl.objective().magnification == 40
    assert ctl.z_um() == pytest.approx(z10 + 11.0)  # 40× parfocal offset vs 10× (simulation profile)
    moves = [e for e in inst.audit.events() if e.command == "focus.move_absolute"]
    assert moves[-2].params["z_um"] == 1500.0  # went to the turret-safe height first
    ph = inst.gateway.read(Role.PHASE_TURRET)
    assert next(p.label for p in ph.get_positions() if p.index == ph.get_position()) == "Ph2"


def test_snap_and_center_on_pixel(inst):
    ctl = inst.controller
    frame = ctl.snap().result(timeout=10)
    assert frame.camera_role == "camera.color" and frame.simulated
    x0, y0 = ctl.xy_um()
    w, h = frame.pixels.shape[1], frame.pixels.shape[0]
    ctl.center_on_pixel(w * 0.75, h / 2, w, h)
    ctl.wait_idle()
    import time

    deadline = time.monotonic() + 2
    while inst.gateway.read(Role.XY_STAGE).is_busy() and time.monotonic() < deadline:
        time.sleep(0.01)
    x1, y1 = ctl.xy_um()
    assert x1 - x0 == pytest.approx(w * 0.25 * ctl.pixel_size_um(), rel=1e-6) and y1 == y0


def test_operator_stop_turns_light_off_and_blocks_until_cleared(inst):
    ctl = inst.controller
    ctl.set_live(True)
    report = ctl.stop()
    assert "camera.color" in report.halted and not ctl.is_live() and not ctl.light_on
    with pytest.raises(CommandRejected):
        ctl.jog_xy(10.0, 0.0)
    ctl.clear_stop()
    ctl.jog_xy(10.0, 0.0)


# ------------------------------------------------------------------------------ acquisition
def test_plan_arithmetic_and_validation():
    plan = AcquisitionPlan(
        channels=[ChannelStep("GFP", 50, 40), ChannelStep("DAPI", 50, 40)],
        positions=[StagePosition("a", 1, 2), StagePosition("b", 3, 4), StagePosition("c", 5, 6)],
        z_stack=ZStack(4.0, 2.0), time_lapse=TimeLapse(4, 60))
    assert plan.z_offsets() == [-2.0, 0.0, 2.0]
    assert plan.image_count() == 2 * 3 * 3 * 4
    assert plan.estimated_duration_s() >= 3 * 60
    assert AcquisitionPlan(channels=[]).validate() == ["select at least one channel"]
    bad = AcquisitionPlan(channels=[ChannelStep("Nope", -1, 120)], z_stack=ZStack(4, 0))
    errs = bad.validate({"GFP"})
    assert any("unknown channel" in e for e in errs) and any("exposure" in e for e in errs)
    assert any("intensity" in e for e in errs) and any("Z step" in e for e in errs)


def test_acquisition_runs_every_image_through_the_policy(inst, tmp_path):
    ctl, gw = inst.controller, inst.gateway
    z_ref = ctl.z_um()
    x0, y0 = ctl.xy_um()
    plan = AcquisitionPlan(
        channels=[ChannelStep("Phase contrast", 20, 40), ChannelStep("GFP", 80, 50, 6.0)],
        positions=[StagePosition("A", x0, y0), StagePosition("B", x0 + 800, y0 - 300)],
        z_stack=ZStack(4.0, 2.0), time_lapse=TimeLapse(2, 0.0))
    seen = []
    res = ctl.run_acquisition(plan, on_frame=seen.append, save_dir=tmp_path).result(timeout=60)
    assert res.status == "completed", res.error
    assert res.images == len(seen) == plan.image_count() == 24
    gfp = [a for a in seen if a.channel == "GFP"]
    assert all(a.frame.exposure_ms == 80 and a.frame.gain_db == 6.0 for a in gfp)
    assert all(a.frame.metadata["fluorescence"] == "GFP" for a in gfp)
    assert sorted({round(a.z_um - z_ref, 6) for a in seen}) == [-2.0, 0.0, 2.0]
    assert {a.position for a in seen} == {"A", "B"}
    assert leds_on(gw) == [] and ctl.z_um() == pytest.approx(z_ref)
    assert gw.policy.exclusive_owner is None
    acq_events = [e for e in inst.audit.events() if e.actor == "acquisition"]
    snaps = [e for e in acq_events if e.command == "camera.snap"]
    assert len(snaps) == 24 and all(e.owner and e.owner.startswith("acquisition:") for e in snaps)
    # saved output
    out = res.save_dir
    assert json.loads((out / "plan.json").read_text("utf-8"))["channels"][1]["preset"] == "GFP"
    lines = (out / "frames.jsonl").read_text("utf-8").splitlines()
    assert len(lines) == 24 and len(list(out.glob("*.npy"))) == 24
    rec = json.loads(lines[-1])
    arr = np.load(out / rec["file"])
    assert arr.shape == (964, 1296) and rec["simulated"] is True


def test_operator_cannot_move_stage_while_acquisition_holds_it(inst):
    ctl, gw = inst.controller, inst.gateway
    errors = []

    def on_frame(_acq):
        try:
            gw.execute("xy.move_relative", dx_um=10.0, dy_um=0.0)
        except CommandRejected as exc:
            errors.append(exc.check)

    plan = AcquisitionPlan(channels=[ChannelStep("DAPI", 10, 30)])
    res = ctl.run_acquisition(plan, on_frame=on_frame).result(timeout=30)
    assert res.status == "completed" and errors == ["exclusive"]


def test_stop_cancels_a_running_acquisition(inst):
    ctl, gw = inst.controller, inst.gateway

    def on_frame(acq):
        if acq.frame and ctl.gw.stop_generation == 0 and len(frames) == 2:
            ctl.stop("test STOP")
        frames.append(acq)

    frames: list = []
    plan = AcquisitionPlan(channels=[ChannelStep("DAPI", 10, 30), ChannelStep("GFP", 10, 30)],
                           z_stack=ZStack(10, 1), time_lapse=TimeLapse(3, 0))
    res = ctl.run_acquisition(plan, on_frame=on_frame).result(timeout=30)
    assert res.status == "cancelled"
    assert res.images == 3 < plan.image_count()
    assert leds_on(gw) == [] and gw.stop_latched and gw.policy.exclusive_owner is None
    end = [e for e in inst.audit.events() if e.command == "acquisition.end"][-1]
    assert end.params["status"] == "cancelled"
