"""Simulated instrument: shape, motion timing, image formation, incubator dynamics."""

from __future__ import annotations

import time

import pytest

np = pytest.importorskip("numpy")

from openevos.backends.simulation import (
    FakeClock,
    SimulationConfig,
    build_simulated_instrument,
    create_simulated_instrument,
)
from openevos.backends.simulation.specimen import Specimen
from openevos.hal.interfaces import DeviceError
from openevos.hal.status_model import RuntimeState
from openevos.hal.types import Role


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def sim(clock):
    s = build_simulated_instrument(SimulationConfig(time_scale=1.0), clock=clock)
    for dev in s.registry.devices():
        dev.connect()
    return s


def sharpness(img: np.ndarray, b: int = 4) -> float:
    """Gradient energy after b×b binning (binning suppresses per-pixel shot noise)."""
    h, w = (img.shape[0] // b) * b, (img.shape[1] // b) * b
    a = img[:h, :w].astype(np.float64).reshape(h // b, b, w // b, b).mean(axis=(1, 3))
    return float(np.mean(np.diff(a, axis=0) ** 2) + np.mean(np.diff(a, axis=1) ** 2))


def bf_mono(sim, exposure=10.0, intensity=30.0):
    sim.illumination.set_led("TL", True, intensity)
    sim.mono.set_exposure_ms(exposure)


# ------------------------------------------------------------------------------ shape
def test_every_device_is_labelled_simulated(sim):
    assert len(sim.registry) == 8
    for role, dev in sim.registry.items():
        ident = dev.identify()
        assert ident.simulated is True and ident.backend == "simulation"
        assert "(simulated)" in ident.display_name
        assert "nothing about it is verified" in ident.notes
        st = dev.get_status()
        assert st.simulated and st.evidence_stage is None and st.state is RuntimeState.READY, role


def test_instrument_shape_follows_vendor_expectations(sim):
    caps = sim.mono.get_capabilities()
    assert (caps.width, caps.height, caps.bit_depth, caps.color) == (1296, 964, 12, False)
    assert len(sim.turret.get_positions()) == 5
    carriage = sim.illumination.get_carriage_positions()
    assert [p.index for p in carriage] == [1, 2, 3, 4, 5] and carriage[-1].label == "Mirror"
    assert [c.metadata["cube"] for c in carriage[:4]] == ["DAPI", "GFP", "RFP", "Cy5"]
    assert sim.phase.get_positions()[0].label == "Open"
    leds = sim.illumination.get_channels()
    assert [c.channel_id for c in leds] == ["TL", "CUBE1", "CUBE2", "CUBE3", "CUBE4"]
    assert [c.carriage_slot for c in leds] == [None, 1, 2, 3, 4]
    assert set(sim.incubator.get_supported_controls()) == {"temperature", "co2", "o2"}


def test_colour_camera_resolution_is_configurable(clock):
    s = build_simulated_instrument(SimulationConfig(color_resolution=(640, 480), time_scale=0.0), clock=clock)
    s.color.connect()
    s.illumination.connect()
    s.illumination.set_led("TL", True, 30.0)
    f = s.color.snap()
    assert f.pixels.shape == (480, 640, 3) and f.pixels.dtype == np.uint8 and f.bit_depth == 8
    assert f.simulated and f.metadata["simulated"] is True
    with pytest.raises(ValueError):
        build_simulated_instrument(SimulationConfig(color_resolution=(10, 10)))


def test_backend_factory_accepts_options():
    reg = create_simulated_instrument({"seed": 7, "time_scale": 0.0, "color_resolution": (320, 240)})
    assert reg.require(Role.CAMERA_COLOR).get_capabilities().width == 320


# ------------------------------------------------------------------------------ motion
def test_xy_motion_is_timed_and_stoppable(sim, clock):
    stage = sim.stage
    stage.move_absolute_um(75000.0, 37500.0)  # 20 mm at 20 mm/s
    assert stage.is_busy() and stage.get_status().state is RuntimeState.BUSY
    clock.advance(0.5)
    x, _ = stage.get_position_um()
    assert 64000 < x < 66000
    stage.stop()
    clock.advance(5)
    assert stage.get_position_um()[0] == x and not stage.is_busy()
    stage.move_absolute_um(1000.0, 2000.0)
    clock.advance(10)
    assert stage.get_position_um() == (1000.0, 2000.0)


def test_backend_enforces_its_own_limits(sim):
    with pytest.raises(DeviceError):
        sim.stage.move_absolute_um(-1.0, 100.0)
    with pytest.raises(DeviceError):
        sim.focus.move_absolute_um(9000.1)
    sim.stage.disconnect()
    with pytest.raises(DeviceError):
        sim.stage.move_absolute_um(10.0, 10.0)


def test_turret_reports_old_position_until_rotation_completes(sim, clock):
    assert sim.turret.get_position() == 3
    sim.turret.select_position(5)
    assert sim.turret.is_busy() and sim.turret.get_position() == 3
    clock.advance(2)
    assert not sim.turret.is_busy() and sim.turret.get_position() == 5
    assert sim.turret.current_objective().magnification == 40


# ------------------------------------------------------------------------------ images
def test_focus_blur_peaks_at_the_focal_plane(sim, clock):
    sim.illumination.select_carriage_position(1)
    clock.advance(2)
    sim.illumination.set_led("CUBE1", True, 60.0)  # DAPI: nuclei
    sim.mono.set_exposure_ms(150.0)
    z0 = sim.config.start_z_um
    scores = {}
    for dz in (-30.0, -8.0, 0.0, 8.0, 30.0):
        sim.focus.move_absolute_um(z0 + dz)
        clock.advance(1)
        f = sim.mono.snap()
        assert f.metadata["sim_defocus_um"] == pytest.approx(dz)
        scores[dz] = sharpness(f.pixels)
    assert scores[0.0] == max(scores.values())
    assert scores[0.0] > 1.3 * scores[8.0] and scores[0.0] > 1.3 * scores[-8.0]
    assert scores[8.0] > scores[30.0] and scores[-8.0] > scores[-30.0]


def test_signal_scales_with_exposure_and_gain(sim):
    bf_mono(sim, exposure=5.0)
    off = sim.mono.model.offset_adu
    m5 = sim.mono.snap().pixels.mean() - off
    sim.mono.set_exposure_ms(10.0)
    m10 = sim.mono.snap().pixels.mean() - off
    sim.mono.set_gain_db(6.0)
    m10g = sim.mono.snap().pixels.mean() - off
    assert m10 / m5 == pytest.approx(2.0, rel=0.05)
    assert m10g / m10 == pytest.approx(10 ** (6 / 20), rel=0.05)


def test_saturation_clips_at_bit_depth(sim):
    bf_mono(sim, exposure=500.0, intensity=100.0)
    px = sim.mono.snap().pixels
    assert px.max() == 4095 and (px == 4095).mean() > 0.5


def test_fluorescence_needs_its_cube_in_the_light_path(sim, clock):
    sim.mono.set_exposure_ms(100.0)
    dark = sim.mono.snap().pixels
    assert abs(dark.mean() - sim.mono.model.offset_adu) < 3  # no light at all
    sim.illumination.set_led("CUBE1", True, 50.0)  # DAPI LED, but carriage is at the mirror
    assert abs(sim.mono.snap().pixels.mean() - dark.mean()) < 3
    sim.illumination.all_leds_off()
    sim.illumination.select_carriage_position(1)
    clock.advance(2)
    sim.illumination.set_led("CUBE1", True, 50.0)
    dapi = sim.mono.snap()
    assert dapi.metadata["fluorescence"] == "DAPI"
    assert dapi.pixels.max() > dark.max() + 300
    sim.illumination.all_leds_off()
    sim.illumination.select_carriage_position(2)
    clock.advance(2)
    sim.illumination.set_led("CUBE2", True, 50.0)
    gfp = sim.mono.snap()
    assert gfp.metadata["fluorescence"] == "GFP"
    corr = np.corrcoef(dapi.pixels.ravel().astype(float), gfp.pixels.ravel().astype(float))[0, 1]
    assert corr < 0.8  # different labels, different structures


def test_light_path_blocked_while_turret_rotates(sim, clock):
    bf_mono(sim)
    sim.focus.move_absolute_um(1000.0)
    clock.advance(2)
    sim.turret.select_position(1)
    f = sim.mono.snap()
    assert "blocked" in f.metadata["light_path"]
    assert abs(f.pixels.mean() - sim.mono.model.offset_adu) < 3


def test_phase_contrast_needs_the_matching_annulus(sim, clock):
    sim.illumination.set_led("TL", True, 45.0)
    sim.color.set_exposure_ms(25.0)
    assert sim.color.snap().metadata["transmitted_mode"] == "brightfield"
    ph1 = next(p.index for p in sim.phase.get_positions() if p.label == "Ph1")  # 10× uses Ph1
    sim.phase.select_position(ph1)
    clock.advance(2)
    assert sim.color.snap().metadata["transmitted_mode"] == "phase contrast"
    ph2 = next(p.index for p in sim.phase.get_positions() if p.label == "Ph2")
    sim.phase.select_position(ph2)
    clock.advance(2)
    assert sim.color.snap().metadata["transmitted_mode"].startswith("phase ring mismatch")


def test_specimen_is_deterministic_per_seed():
    a = Specimen(1, 300).cells_in(0, 2000, 0, 2000)
    b = Specimen(1, 300).cells_in(0, 2000, 0, 2000)
    c = Specimen(2, 300).cells_in(0, 2000, 0, 2000)
    assert len(a) > 100 and np.array_equal(a.cx, b.cx)
    assert not (len(a) == len(c) and np.array_equal(a.cx, c.cx))


def test_live_mode_streams_frames():
    s = build_simulated_instrument(SimulationConfig(time_scale=0.0))
    for d in s.registry.devices():
        d.connect()
    s.illumination.set_led("TL", True, 30.0)
    s.mono.start_live()
    deadline = time.monotonic() + 5
    while (s.mono.get_latest_frame() is None or s.mono.get_latest_frame().index < 3) \
            and time.monotonic() < deadline:
        time.sleep(0.02)
    assert s.mono.is_live() and s.mono.get_latest_frame().index >= 3
    with pytest.raises(DeviceError):
        s.mono.snap()
    s.mono.halt()
    assert not s.mono.is_live()


# ------------------------------------------------------------------------------ incubator
def test_incubator_moves_towards_setpoint_only_when_enabled(sim, clock):
    inc = sim.incubator
    t0 = inc.read_telemetry()
    assert all(r.source == "simulated" for r in t0.readings.values()) and t0.simulated
    assert t0.readings["temperature"].value == pytest.approx(23.5, abs=0.2)
    inc.set_setpoint("temperature", 37.0)
    clock.advance(3600)
    assert inc.read_telemetry().readings["temperature"].value == pytest.approx(23.5, abs=0.2)
    inc.set_enabled("temperature", True)
    inc.set_enabled("co2", True)
    clock.advance(3 * 3600)
    tel = inc.read_telemetry().readings
    assert tel["temperature"].value == pytest.approx(37.0, abs=0.2)
    assert tel["co2"].value == pytest.approx(5.0, abs=0.2)
    assert tel["o2"].value == pytest.approx(20.9, abs=0.3)
    inc.set_enabled("temperature", False)
    clock.advance(3 * 3600)
    assert inc.read_telemetry().readings["temperature"].value == pytest.approx(23.5, abs=0.3)
    with pytest.raises(DeviceError):
        inc.set_setpoint("temperature", 45.0)
    with pytest.raises(DeviceError):
        inc.set_setpoint("humidity", 50.0)
