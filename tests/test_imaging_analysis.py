"""Composite/save rules, focus metrics, counting, scan and time-lapse planning."""

from __future__ import annotations

import json

import numpy as np
import pytest
from pydantic import ValidationError
from scipy import ndimage

from openevos.acquisition.capture import CapturedImage
from openevos.acquisition.scan_plan import AreaMethod, Beacon, Coverage, ScanRoutine, plan_scan
from openevos.acquisition.timelapse import Scene, TimelapseRoutine, estimate, populate_wells
from openevos.analysis.count import MAX_LABELS, AutoCountParams, ManualCount, auto_count
from openevos.imaging.composite import (
    Layer,
    composite,
    highlight_saturated,
    pseudocolor,
    transfection,
)
from openevos.imaging.focus_metrics import METRICS, parabolic_peak, peak_prominence
from openevos.imaging.save import (
    SaveError,
    SaveOptions,
    SavePreset,
    allowed_bit_depths,
    save,
    validate,
)
from openevos.instrument.profile import CameraRole, load_profile
from openevos.instrument.settings import ImageFormat
from openevos.instrument.vessels import VesselLibrary, VesselStageMap

PROFILE = load_profile()
LIB = VesselLibrary()
P96 = LIB.get("plate-96")


def _img(data, color=False, synthetic=True) -> CapturedImage:
    return CapturedImage(data=data, channel="cube2", channel_label="GFP",
                         camera=CameraRole.COLOR if color else CameraRole.MONO, objective_slot=2, magnification=10,
                         pixel_size_um=0.375, stage_xy_um=(0, 0), z_um=3000, exposure_ms=40, gain=1, led_percent=50,
                         synthetic=synthetic, frame_device="sim-camera", frame_index=1, bit_depth=8 if color else 12)


def _disks(n=12, r=6, shape=(200, 300), seed=1):
    rng = np.random.default_rng(seed)
    img = np.full(shape, 200.0)
    yy, xx = np.mgrid[: shape[0], : shape[1]]
    centres = []
    while len(centres) < n:
        c = (rng.uniform(15, shape[1] - 15), rng.uniform(15, shape[0] - 15))
        if all(np.hypot(c[0] - a, c[1] - b) > 4 * r for a, b in centres):
            centres.append(c)
    for cx, cy in centres:
        img[(xx - cx) ** 2 + (yy - cy) ** 2 <= r * r] = 2000
    return (img + rng.normal(0, 15, shape)).clip(0, 4095).astype(np.uint16)


# ------------------------------------------------------------------ composite
def test_pseudocolor_composite_and_saturation():
    g = np.full((4, 4), 200, np.uint8)
    rgb = pseudocolor(g, "#00FF00")
    assert rgb[0, 0].tolist() == [0, 200, 0]
    a = np.zeros((8, 8), np.uint16)
    a[2:4, 2:4] = 1000
    out = composite([Layer(a, "#FF0000"), Layer(a, "#0000FF")])
    assert out.shape == (8, 8, 3) and out[3, 3, 0] > 0 and out[3, 3, 2] > 0
    with pytest.raises(ValueError):
        composite([Layer(a), Layer(np.zeros((4, 4), np.uint16))])
    raw = np.array([[4095, 0]], np.uint16)
    hl = highlight_saturated(np.zeros((1, 2, 3), np.uint8), raw, 4095)
    assert hl[0, 0].tolist() == [255, 0, 0] and hl[0, 1].tolist() == [0, 0, 0]
    assert transfection(Layer(a, "#00FF00"), Layer(a)).shape == (8, 8, 3)


# ------------------------------------------------------------------ save rules (manual p.10, p.15-17)
def test_save_rule_matrix():
    assert validate(SaveOptions(ImageFormat.TIFF, 16, False, False), source_is_color=False) == []
    assert validate(SaveOptions(ImageFormat.PNG, 16, False, False), source_is_color=False)
    assert validate(SaveOptions(ImageFormat.TIFF, 16, True, False), source_is_color=False)
    assert validate(SaveOptions(ImageFormat.TIFF, 16, False, False), source_is_color=True)
    assert validate(SaveOptions(ImageFormat.TIFF, 16, False, True), source_is_color=False)  # annotations in raw
    assert allowed_bit_depths(ImageFormat.TIFF, False) == (8, 16)
    for fmt in (ImageFormat.PNG, ImageFormat.JPEG, ImageFormat.BMP):
        assert allowed_bit_depths(fmt, False) == (8,)
    assert allowed_bit_depths(ImageFormat.TIFF, True) == (8,)
    d = SavePreset.DEFAULT.options()
    assert (d.image_format, d.bit_depth, d.as_color, d.include_annotations) == (ImageFormat.TIFF, 8, True, True)
    q = SavePreset.QUANTITATIVE_MONO.options()
    assert (q.bit_depth, q.as_color) == (16, False)


def test_save_files_preserve_raw_and_label_simulated(tmp_path):
    import tifffile

    raw = np.arange(48, dtype=np.uint16).reshape(6, 8) * 80
    img = _img(raw)
    out = save(tmp_path, "Raw", rgb8=None, raw=img, opts=SavePreset.QUANTITATIVE_MONO.options())
    back = tifffile.imread(out.image)
    assert back.dtype == np.uint16 and np.array_equal(back, raw)  # 16-bit raw survives untouched
    meta = json.loads(out.sidecar.read_text())
    assert meta["provenance"].startswith("SIMULATED") and meta["primary"]["synthetic"] is True
    rgb = np.zeros((6, 8, 3), np.uint8)
    o2 = save(tmp_path, "Raw", rgb8=rgb, raw=img, opts=SaveOptions(save_underlying=True), channels={"cube2": img})
    assert o2.image.name == "Raw_0002.tif" and len(o2.underlying) == 1
    assert tifffile.imread(o2.underlying[0]).dtype == np.uint16
    with pytest.raises(SaveError):
        save(tmp_path, "x", rgb8=rgb, raw=_img(rgb, color=True), opts=SaveOptions(as_color=False, bit_depth=8,
                                                                                 include_annotations=False))
    png = save(tmp_path, "P", rgb8=rgb, raw=img, opts=SaveOptions(ImageFormat.PNG))
    assert png.image.suffix == ".png"


# ------------------------------------------------------------------ focus metrics
def test_focus_metrics_prefer_sharp():
    sharp = _disks().astype(float)
    blur = ndimage.gaussian_filter(sharp, 4)
    for name, fn in METRICS.items():
        assert fn(sharp) > fn(blur), name
    z = [0, 1, 2, 3, 4]
    s = [1, 3, 5, 3, 1]
    assert parabolic_peak(z, s)[0] == pytest.approx(2.0)
    assert parabolic_peak(z, [1, 3, 5, 4.9, 1])[0] > 2.0
    assert peak_prominence([1, 1, 1, 1]) == 0


# ------------------------------------------------------------------ counting (manual p.13-16)
def test_auto_count_known_disks_and_sensitivity():
    img = _disks(n=12)
    r = auto_count(img, AutoCountParams(), pixel_size_um=0.5)
    assert r.count == 12
    assert all(o.circularity > 0.7 for o in r.objects)
    assert r.objects[0].area_um2 == pytest.approx(r.objects[0].area_px * 0.25)
    strict = auto_count(img, AutoCountParams(area=0))  # min area large -> fewer objects
    assert strict.count < r.count
    assert auto_count(img, AutoCountParams(brightness=90)).threshold < r.threshold
    inv = auto_count((4095 - img).astype(np.uint16), AutoCountParams(invert=True))
    assert inv.count == 12


def test_manual_count_labels():
    mc = ManualCount()
    for i in range(MAX_LABELS - 1):
        mc.add_label(f"L{i}")
    with pytest.raises(ValueError):
        mc.add_label("too many")
    mc.add(10, 10, 0)
    mc.add(20, 20, 1)
    mc.add(21, 20, 1)
    assert mc.counts()["L0"] == 2
    assert mc.remove_near(20.5, 20) and mc.counts()["L0"] == 1
    assert not mc.remove_near(500, 500)


# ------------------------------------------------------------------ scan planning (manual p.22-25)
def _routine(**kw) -> ScanRoutine:
    base = {"name": "t", "vessel_id": "plate-96", "objective_slot": 2, "channels": ["cube2"], "regions": ["D7"]}
    base.update(kw)
    return ScanRoutine(**base)


def test_scan_coverage_modes():
    m = VesselStageMap()
    full = plan_scan(_routine(coverage=Coverage.FULL), P96, PROFILE, m)
    four = plan_scan(_routine(coverage=Coverage.FOUR_POINTS), P96, PROFILE, m)
    grid = plan_scan(_routine(coverage=Coverage.GRID, grid_rows=2, grid_cols=5), P96, PROFILE, m)
    rnd = plan_scan(_routine(coverage=Coverage.RANDOM, random_images=7, random_seed=3), P96, PROFILE, m)
    rnd2 = plan_scan(_routine(coverage=Coverage.RANDOM, random_images=7, random_seed=3), P96, PROFILE, m)
    center = plan_scan(_routine(coverage=Coverage.CENTER, percentage=30), P96, PROFILE, m)
    outer = plan_scan(_routine(coverage=Coverage.OUTER, percentage=30), P96, PROFILE, m)
    assert len(full.tiles) > 20  # a 6.4 mm well at 10x needs many fields
    assert len(four.tiles) == 4 and len(grid.tiles) == 10 and len(rnd.tiles) == 7
    assert [t.x_mm for t in rnd.tiles] == [t.x_mm for t in rnd2.tiles]  # reproducible
    assert 0 < len(center.tiles) < len(full.tiles) and 0 < len(outer.tiles) < len(full.tiles)
    assert {t.region_id for t in full.tiles} == {"D7"}
    two = plan_scan(_routine(coverage=Coverage.FOUR_POINTS, regions=["A1", "B2"]), P96, PROFILE, m)
    assert len(two.tiles) == 8
    assert any("nominal" in w for w in full.warnings)
    assert full.est_bytes == len(full.tiles) * 648 * 482 * 2


def test_scan_beacons_limits_and_camera_rules():
    with pytest.raises(ValidationError, match="two beacons"):
        _routine(area_method=AreaMethod.BEACONS, beacons=[Beacon(label="a", x_mm=1, y_mm=1)])
    b = [Beacon(label="1", x_mm=60, y_mm=40), Beacon(label="2", x_mm=61, y_mm=40.6)]
    p = plan_scan(_routine(area_method=AreaMethod.BEACONS, beacons=b), P96, PROFILE, VesselStageMap())
    assert p.tiles and all(60 - 0.3 <= t.x_mm <= 61 + 0.3 for t in p.tiles)
    tight = ((-1000.0, 1000.0), (-1000.0, 1000.0))
    p2 = plan_scan(_routine(regions=["A1"], coverage=Coverage.FOUR_POINTS), P96, PROFILE, VesselStageMap(), tight)
    assert p2.out_of_limits == 4 and any("stage limits" in w for w in p2.warnings)
    with pytest.raises(ValueError, match="not available"):
        plan_scan(_routine(camera=CameraRole.COLOR), P96, PROFILE, VesselStageMap())


# ------------------------------------------------------------------ time-lapse (manual p.28-32)
def test_timelapse_rules_and_estimate():
    beacons = populate_wells(P96)
    assert len(beacons) == 96 and beacons[0].label == "A1"
    assert len(populate_wells(LIB.get("plate-384"))) == 384
    with pytest.raises(ValidationError, match="384"):
        TimelapseRoutine(name="x", vessel_id="plate-384", objective_slot=1, channels=["trans"],
                         beacons=beacons * 5, scenes=[Scene(duration_s=60, interval_s=10)])
    with pytest.raises(ValidationError, match="single scene"):
        TimelapseRoutine(name="x", vessel_id="plate-96", objective_slot=1, channels=["trans"], beacons=beacons[:2],
                         scenes=[Scene(duration_s=60, interval_s=10)] * 2, capture_one_frame_only=True)
    r = TimelapseRoutine(name="x", vessel_id="plate-96", objective_slot=1, channels=["cube2", "trans"],
                         beacons=beacons, scenes=[Scene(duration_s=3600, interval_s=60, temperature_c=37,
                                                        co2_percent=5)])
    e = estimate(r, PROFILE)
    assert e.timepoints == 61 and e.images == 61 * 96 * 2
    assert any("late" in w for w in e.warnings)  # 96 positions cannot fit a 60 s interval
    assert any("recorded only" in w for w in e.warnings)  # incubator unavailable
    assert Scene(duration_s=600, interval_s=60).timepoints == 11


def test_count_percentages_and_aggressive_boundaries():
    mc = ManualCount(labels=["live", "dead"])
    for _ in range(3):
        mc.add(1, 1, 0)
    mc.add(5, 5, 1)
    assert mc.percentages() == {"live": 75.0, "dead": 25.0}
    img = ndimage.gaussian_filter(_disks(n=10, r=8).astype(float), 3).astype(np.uint16)  # soft edges
    normal = auto_count(img, AutoCountParams())
    aggressive = auto_count(img, AutoCountParams(aggressive=True))
    assert aggressive.count >= normal.count
    assert np.mean([o.area_px for o in aggressive.objects]) < np.mean([o.area_px for o in normal.objects])


def test_saved_image_reopens_with_metadata(tmp_path):
    pytest.importorskip("PySide6")  # loader lives in the UI layer
    from openevos.ui.pages.workspaces import load_image_file

    raw = _disks(n=4)
    out = save(tmp_path, "R", rgb8=None, raw=_img(raw), opts=SavePreset.QUANTITATIVE_MONO.options())
    back = load_image_file(out.image)
    assert np.array_equal(back.data, raw) and back.synthetic and back.pixel_size_um == 0.375
    assert back.objective_slot == 2 and back.z_um == 3000
