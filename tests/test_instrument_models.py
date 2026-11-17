"""Instrument profile, vessels, settings, readiness — pure models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from openevos.instrument.profile import CameraRole, InstrumentProfile, Provenance, load_profile
from openevos.instrument.readiness import PHYSICAL_READINESS, Readiness, readiness_for
from openevos.instrument.settings import (
    AppSettings,
    HotButtonAction,
    MonoSettings,
    QuickSaveSettings,
    light_slider_split,
)
from openevos.instrument.vessels import VesselLibrary, VesselStageMap


def test_profile_dual_camera_rules():
    p = load_profile()
    assert p.backend == "simulation"
    mono = [c.slot for c in p.channels_for(CameraRole.MONO)]
    color = [c.slot for c in p.channels_for(CameraRole.COLOR)]
    assert {"cube1", "cube2", "cube3", "trans", "phase"} <= set(mono)
    assert color == ["trans", "phase"]  # colour camera: brightfield/phase only (manual p.9)
    assert "cube4" not in mono  # empty slot
    assert all(c.provenance is Provenance.SIMULATED for c in p.cameras)


def test_profile_shape_validation():
    d = load_profile().model_dump()
    d["channels"] = [*d["channels"], dict(d["channels"][0], slot="cube5")]
    with pytest.raises(ValidationError, match="four light cubes"):
        InstrumentProfile.model_validate(d)
    d = load_profile().model_dump()
    d["objectives"] = [*d["objectives"], dict(d["objectives"][0], slot=6)]
    with pytest.raises(ValidationError):
        InstrumentProfile.model_validate(d)


def test_vessel_library_and_mapping():
    lib = VesselLibrary()
    v = lib.get("plate-96")
    assert len(v.regions) == 96 and v.rows == 8 and v.cols == 12
    assert not v.geometry_verified  # nominal geometry is never presented as measured
    h12 = v.region("H12")
    assert v.region_at(h12.cx, h12.cy).region_id == "H12"
    assert v.region_at(h12.cx + 4.4, h12.cy) is None  # between wells
    m = VesselStageMap()
    sx, sy = m.to_stage(h12.cx, h12.cy)
    assert m.to_vessel(sx, sy) == pytest.approx((h12.cx, h12.cy))
    assert abs(sx) <= 50_000 and abs(sy) <= 50_000  # all 96-well centres fit the simulator envelope
    assert len(lib.get("plate-384").regions) == 384


def test_vessel_manager_and_focus_nominal():
    lib = VesselLibrary()
    u = lib.add_user_vessel(lib.get("plate-24"), "costar-24", "24-well Costar")
    assert u.user_created and u.provenance is Provenance.OPERATOR
    with pytest.raises(ValueError):
        lib.add_user_vessel(lib.get("plate-24"), "costar-24", "dup")
    with pytest.raises(ValueError):
        lib.remove_user_vessel("plate-24")
    lib.remove_user_vessel("costar-24")
    assert lib.with_focus_nominal("plate-96", 2950.5).focus_nominal_um == 2950.5


def test_hot_buttons_and_light_slider():
    m = MonoSettings(hot_buttons=[HotButtonAction.CAPTURE])
    assert len(m.hot_buttons) == 4 and m.hot_buttons[1] is HotButtonAction.NONE
    with pytest.raises(ValidationError):
        MonoSettings(hot_buttons=[HotButtonAction.SAVE] * 5)
    assert {a for a in HotButtonAction} - {HotButtonAction.NONE} == {
        HotButtonAction.CAPTURE, HotButtonAction.CAPTURE_ALL, HotButtonAction.TRANSFECTION, HotButtonAction.SAVE,
        HotButtonAction.ZSTACK, HotButtonAction.CAPTURE_FOCUS_NOMINAL, HotButtonAction.QUICK_SAVE}  # manual p.21
    prev = (0.0, 0.0, 0.0)
    for v in range(0, 101, 5):
        cur = light_slider_split(v, True)
        assert all(c >= p for c, p in zip(cur, prev))  # monotonic in every component
        prev = cur
    assert light_slider_split(100, False)[2] > 1


def test_settings_persistence_and_validation(tmp_path):
    s = AppSettings()
    s.mono.capture_all = ["cube2"]
    s.channel(CameraRole.MONO, "cube2")
    path = tmp_path / "ui.json"
    s.save(path)
    s2 = AppSettings.load(path)
    assert s2.mono.capture_all == ["cube2"] and "cube2" in s2.mono.channels
    assert AppSettings.load(tmp_path / "missing.json").schema_version == 1
    with pytest.raises(ValidationError):
        QuickSaveSettings(base_name="a/b")


def test_readiness_reflects_bringup_record():
    assert readiness_for("Objective turret").readiness is Readiness.LOCKED
    assert readiness_for("Phase / condenser wheel").readiness is Readiness.LOCKED
    assert readiness_for("Onstage incubator (AMC1000)").readiness is Readiness.UNAVAILABLE
    assert readiness_for("Monochrome camera").readiness is Readiness.NOT_INTEGRATED
    assert all(r.unlock_requires for r in PHYSICAL_READINESS)
