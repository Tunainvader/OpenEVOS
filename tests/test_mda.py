"""Multi-D acquisition (ADR-0065): plan, compiler, scheduler, runner, experiment writer and page, on the simulator.

Test names carry the kit's acceptance IDs (OpenEVOS Multi-Dimensional Acquisition Kit v1.0, qa matrix) where one
applies. Simulation is not physical qualification: nothing here qualifies stage moves between positions.
"""

from __future__ import annotations

import itertools
import json
import threading
import time
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from openevos.acquisition import mda
from openevos.acquisition.capture import CaptureRejected
from openevos.acquisition.mda import (
    MAX_EVENTS,
    ChannelPlan,
    FocusPolicy,
    Missed,
    MultiDimPlan,
    PositionPlan,
    Schedule,
    TimeAxisPlan,
    Traversal,
    ZAxisPlan,
    compile_events,
    compile_jobs,
    preflight,
    summarize,
    validate,
)
from openevos.acquisition.mda_runner import MDAState, MultiDimRunner
from openevos.acquisition.scan_runner import RunControl
from openevos.acquisition.session import build_sim_session
from openevos.core.clock import ThreadClock
from openevos.imaging.mda_store import ExperimentReader, ExperimentWriter, StorageFull
from openevos.instrument.profile import CameraRole
from openevos.instrument.settings import AppSettings

MONO = CameraRole.MONO
CH = (ChannelPlan("cube1", "DAPI", 20, 1, 50), ChannelPlan("cube2", "GFP", 20, 1, 50))


def mkplan(*, n_t=1, n_p=1, channels=CH, below=2.0, above=2.0, step=1.0, z_on=True, anchor=1000.0,
           traversal=Traversal.T_P_C_Z, interval=10.0, schedule=Schedule.MIN_INTERVAL, missed=Missed.SKIP,
           positions=None, **kw) -> MultiDimPlan:
    if positions is None:
        positions = tuple(PositionPlan(f"P{i + 1:04d}", f"Field {i + 1}", 100.0 * i, 0.0, "um", "sim-stage")
                          for i in range(n_p)) if n_p > 1 else (PositionPlan("P0001", "Here"),)
    return MultiDimPlan(channels=channels, camera=MONO,
                        z=ZAxisPlan(enabled=z_on, below_um=below, above_um=above, interval_um=step),
                        time=TimeAxisPlan(n_t > 1, n_t, interval, schedule, missed), positions=positions,
                        traversal=traversal, global_anchor_z_um=anchor, **kw)


# ====================================================================================== plan and compiler
def test_p001_compile_z_by_c_exact_events():
    p = mkplan()
    ev = list(compile_events(p))
    assert len(ev) == p.n_events == 2 * 5
    assert {(e.c_index, e.z_index) for e in ev} == {(c, z) for c in range(2) for z in range(5)}


def test_p002_p003_compile_with_time_and_positions_unique_and_counted():
    p = mkplan(n_t=3, n_p=4)
    ev = list(compile_events(p))
    assert len(ev) == 3 * 4 * 2 * 5 == p.n_events
    keys = {(e.t_index, e.p_index, e.c_index, e.z_index) for e in ev}
    assert len(keys) == len(ev)
    assert len({e.event_id for e in ev}) == len(ev)  # P009: no duplicate event IDs
    assert {e.position_id for e in ev} == {"P0001", "P0002", "P0003", "P0004"}


def test_p004_recompile_identical_plan_same_ids_and_order():
    p = mkplan(n_t=2, n_p=2)
    a = [e.event_id for e in compile_events(p)]
    b = [e.event_id for e in compile_events(MultiDimPlan.from_json(p.to_json()))]
    assert a == b
    assert a[0] == "T0001P0001C01Z0001"


def test_t001_t002_traversal_orders():
    cz = [(e.t_index, e.p_index, e.c_index, e.z_index) for e in compile_events(mkplan(n_t=2, n_p=2))]
    assert cz == sorted(cz)  # T, then P, then the whole Z range per channel
    zc = [(e.t_index, e.p_index, e.z_index, e.c_index)
          for e in compile_events(mkplan(n_t=2, n_p=2, traversal=Traversal.T_P_Z_C))]
    assert zc == sorted(zc)  # every channel at each plane before the next plane
    jobs = list(compile_jobs(mkplan(n_t=2, n_p=3)))
    assert [(j.t_index, j.p_index) for j in jobs] == [(t, p) for t in range(2) for p in range(3)]


def test_p010_compiler_is_lazy_for_large_plans():
    p = mkplan(n_t=2000, n_p=1, step=0.5, below=10, above=10)  # 2000 x 2 x 41
    assert p.n_events > 100_000 - 1 or validate(p)
    first = list(itertools.islice(compile_events(p), 3))
    assert [e.event_id for e in first] == ["T0001P0001C01Z0001", "T0001P0001C01Z0002", "T0001P0001C01Z0003"]
    assert any("limit" in e for e in validate(mkplan(n_t=10000, step=0.1, below=10, above=10)))
    assert MAX_EVENTS == 100_000


@pytest.mark.parametrize("plan_kw, words", [
    ({"channels": ()}, "channel"),
    ({"step": 0.0}, "step"),
    ({"n_t": 3, "interval": 0.0}, "interval"),
    ({"anchor": None}, "focus reference"),
    ({"positions": (PositionPlan("P0001", "A", 1.0, 2.0, "um", ""),)}, "coordinate frame"),
    ({"positions": (PositionPlan("P0001", "A", 1.0, None, "um", "f"),)}, "X and Y"),
    ({"positions": (PositionPlan("P0001", "A"), PositionPlan("P0001", "B"))}, "same id"),
    ({"channels": (CH[0], CH[0])}, "twice"),
])
def test_p005_p007_p008_invalid_plans_are_rejected_before_hardware(plan_kw, words):
    errs = validate(mkplan(**plan_kw))
    assert any(words in e for e in errs), errs


def test_s007_per_position_focus_needs_every_anchor():
    pos = (PositionPlan("P0001", "A", 0, 0, "um", "f", z_anchor_um=10.0), PositionPlan("P0002", "B", 1, 0, "um", "f"))
    p = mkplan(positions=pos, focus_policy=FocusPolicy.PER_POSITION)
    assert any("B: no focus reference" in e for e in validate(p))
    with pytest.raises(CaptureRejected):
        list(compile_jobs(p))


def test_p006_unsupported_traversal_and_schema_rejected():
    d = mkplan().to_dict()
    d["traversal"] = "position_time_channel_z"
    with pytest.raises(ValueError):
        MultiDimPlan.from_dict(d)
    d = mkplan().to_dict()
    d["schema_version"] = 99
    with pytest.raises(ValueError):
        MultiDimPlan.from_dict(d)


def test_plan_round_trip_and_hash_ignores_identity():
    p = mkplan(n_t=3, n_p=2, name="exp")
    q = MultiDimPlan.from_json(p.to_json())
    assert q == p
    f = p.fresh()
    assert f.plan_id != p.plan_id and f.content_hash() == p.content_hash()
    assert replace(p, name="other").content_hash() == p.content_hash()
    assert mkplan(n_t=4, n_p=2).content_hash() != p.content_hash()


def test_summary_is_dtype_aware_and_says_its_source(sim):
    p = mkplan(n_t=2, n_p=2)
    s = summarize(p, sim.profile)
    cam = sim.profile.camera(MONO)
    assert s.images == p.n_events
    assert s.frame_shape == (cam.height, cam.width) and s.frame_source == "camera profile"
    assert s.bytes_per_image == cam.height * cam.width * (2 if cam.native_bit_depth > 8 else 1)
    assert s.total_s > 0 and len(s.preview) == 8
    assert summarize(p, None).frame_source.startswith("default")


# ====================================================================================== scheduler (fake clock)
class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


class FakeControl(RunControl):
    def __init__(self, clock: FakeClock):
        super().__init__()
        self.clock = clock

    def sleep(self, s: float) -> None:  # time passes instantly
        self.checkpoint()
        self.clock.t += s


class _Stub:
    capture = controller = None


def _sched(plan, clock, prev_start=0.0, t=1):
    ctl = FakeControl(clock)
    r = MultiDimRunner(_Stub(), ExperimentWriter(Path("/nonexistent")), control=ctl, monotonic=clock)
    return r._schedule(plan, t, 0.0, prev_start, lambda **_k: None)


def test_t003_min_interval_waits_or_starts_late():
    p = mkplan(n_t=3, interval=10.0)
    c = FakeClock()
    c.t = 4.0
    skip, rec = _sched(p, c)
    assert not skip and rec["status"] == "run" and c.t == pytest.approx(10.0)  # waited to start + interval
    c.t = 17.0  # the cycle ran 17 s: start at once, marked late
    skip, rec = _sched(p, c)
    assert not skip and rec["status"] == "late" and rec["late_s"] == pytest.approx(7.0) and c.t == 17.0


def test_t004_fixed_schedule_skips_missed_without_burst():
    p = mkplan(n_t=5, interval=10.0, schedule=Schedule.FIXED, missed=Missed.SKIP)
    c = FakeClock()
    c.t = 13.0  # timepoint 1 was due at 10 s
    skip, rec = _sched(p, c, t=1)
    assert skip and rec["status"] == "skipped"
    skip, rec = _sched(p, c, t=2)  # due at 20 s: waits for it, no catch-up burst
    assert not skip and c.t == pytest.approx(20.0) and rec["scheduled_offset_s"] == pytest.approx(20.0)


def test_t005_fixed_schedule_run_late_once():
    p = mkplan(n_t=5, interval=10.0, schedule=Schedule.FIXED, missed=Missed.RUN_LATE_ONCE)
    c = FakeClock()
    c.t = 13.0
    skip, rec = _sched(p, c, t=1)
    assert not skip and rec["status"] == "late" and rec["late_s"] == pytest.approx(3.0)
    assert rec["scheduled_offset_s"] == pytest.approx(10.0) and rec["started_offset_s"] == pytest.approx(13.0)
    c.t = 25.0  # so late the next target (20 s) has passed too: skipped, not run twice
    skip, rec = _sched(p, c, t=1)
    assert skip


def test_t006_pause_shifts_min_interval_but_not_fixed():
    for schedule, expect in ((Schedule.MIN_INTERVAL, 15.0), (Schedule.FIXED, 10.0)):
        p = mkplan(n_t=3, interval=10.0, schedule=schedule)
        c = FakeClock()
        ctl = FakeControl(c)
        r = MultiDimRunner(_Stub(), ExperimentWriter(Path("/nonexistent")), control=ctl, monotonic=c)
        ctl.pause()

        def unpause(ctl=ctl, c=c):
            time.sleep(0.2)
            c.t += 5.0  # five seconds paused
            ctl.resume()

        threading.Thread(target=unpause, daemon=True).start()
        r._schedule(p, 1, 0.0, 0.0, lambda **_k: None)
        assert c.t == pytest.approx(expect), schedule


# ====================================================================================== simulator end to end
@pytest.fixture
def sim():
    s = build_sim_session(ThreadClock(), fast=True, settings=AppSettings())
    time.sleep(0.3)
    yield s
    s.shutdown()


def simplan(sim, *, n_t=1, n_p=1, interval=0.5, **kw) -> MultiDimPlan:
    xy = sim.controller.stage_xy()
    unit, frame = sim.controller.stage_frame()
    pos = tuple(PositionPlan(f"P{i + 1:04d}", f"Field {i + 1}", xy[0] + 400 * i, xy[1], unit, frame)
                for i in range(n_p)) if n_p > 1 else None
    return mkplan(n_t=n_t, interval=interval, positions=pos, anchor=sim.controller.focus_z(), below=1, above=1,
                  simulated=True, instrument=sim.profile.name, **kw)


def run(sim, plan, root, *, writer=None, progress=None, on_image=None, control=None):
    s = summarize(plan, sim.profile)
    pre = preflight(plan, sim, s, save_root=root)
    assert pre.ok, pre.errors
    w = writer or ExperimentWriter(root)
    r = MultiDimRunner(sim, w, control=control)
    return r, r.run(plan, s, pre, progress=progress, on_image=on_image)


def test_end_to_end_zctp_writes_a_complete_readable_experiment(sim, tmp_path):
    import tifffile

    p = simplan(sim, n_t=2, n_p=2)
    _r, res = run(sim, p, tmp_path)
    assert res.state is MDAState.COMPLETED and res.images_written == p.n_events == 2 * 2 * 2 * 3
    folder = Path(res.folder)
    for f in ("experiment.json", "plan.json", "manifest.json", "events.jsonl"):
        assert (folder / f).exists()
    rd = ExperimentReader(folder)
    imgs = [e for e in rd.events() if e["kind"] == "image"]
    ids = [e["event_id"] for e in imgs]
    assert len(ids) == len(set(ids)) == p.n_events  # D001: each persisted frame maps to one event id
    assert set(ids) == {e.event_id for e in compile_events(p)}
    # D009: simulation provenance at manifest and image level
    assert rd.experiment["simulated"] is True and "SIMULATED" in rd.experiment["provenance"]
    vol = rd.load(1, 1)
    assert vol["record"]["synthetic"] is True and all(e["synthetic"] for e in imgs)
    # D007 / D008: P is folder + metadata, not an image axis; planned and observed coordinates both kept
    assert vol["record"]["mda"]["position"]["id"] == "P0002"
    assert {"x_planned", "x_observed", "z_planned_um", "z_observed_um"} <= set(imgs[0])
    plan_json = json.loads((folder / "plan.json").read_text())
    assert plan_json["plan_sha256"] == p.content_hash()
    # D006: the OME-TIFF reads back outside OpenEVOS with Z and C axes and per-plane PositionZ
    f = folder / "Positions/P0002_Field_2/T000002/stack.ome.tif"
    with tifffile.TiffFile(f) as tf:
        assert tf.series[0].axes == "ZCYX" and tf.series[0].shape[:2] == (3, 2)
        assert "PositionZ" in tf.ome_metadata
    assert vol["stacks"]["cube1"][0].shape[0] == 3
    # D010: the projection says what it came from
    with tifffile.TiffFile(folder / "Positions/P0002_Field_2/T000002/cube1_max.tif") as tf:
        desc = json.loads(tf.pages[0].description)
    assert desc["projection"] == "max" and desc["slices"] == 3
    tps = rd.manifest["timepoints"]
    assert [t["t_index"] for t in tps] == [0, 1] and all("finished_at" in t for t in tps)


def test_a004_stop_and_keep_writes_a_partial_manifest(sim, tmp_path):
    p = simplan(sim, n_t=3, n_p=2)
    holder: dict = {}

    def on_image(job, slot, zi, done, img):
        if done == 8:
            holder["r"].stop()

    s = summarize(p, sim.profile)
    pre = preflight(p, sim, s, save_root=tmp_path)
    r = MultiDimRunner(sim, ExperimentWriter(tmp_path))
    holder["r"] = r
    res = r.run(p, s, pre, on_image=on_image)
    assert res.state is MDAState.PARTIAL and "stopped" in res.message
    assert 8 <= res.images_written < p.n_events
    rd = ExperimentReader(Path(res.folder))  # D005: reopen a partial experiment
    st = {v["key"]: v["status"] for v in rd.manifest["volumes"]}
    assert st["T000001P0001"] == "completed" and "partial" in st.values() and "not acquired" in st.values()
    assert rd.manifest["status"] == "partial" and rd.manifest["volumes_missing"]
    assert rd.load(2, 1) is None
    assert len([e for e in rd.events() if e["kind"] == "image"]) == res.images_written


def test_a003_pause_and_resume_take_no_frame_twice(sim, tmp_path):
    p = simplan(sim, n_t=2, n_p=1, interval=0.2)
    ctl = RunControl()
    seen: list = []

    def on_image(job, slot, zi, done, img):
        seen.append((job.t_index, slot, zi))
        if done == 2 and not ctl.paused:
            ctl.pause()
            threading.Timer(0.6, ctl.resume).start()

    _r, res = run(sim, p, tmp_path, control=ctl, on_image=on_image)
    assert res.state is MDAState.COMPLETED and res.images_written == p.n_events
    assert len(seen) == len(set(seen)) == p.n_events
    states = [e.get("state") for e in ExperimentReader(Path(res.folder)).events() if e["kind"] == "state"]
    assert states == [] or "paused" in states  # paused inside a volume (Z-stack runner) or between volumes


def test_d003_disk_full_stops_safely_keeping_committed_volumes(sim, tmp_path):
    p = simplan(sim, n_t=3, n_p=1, interval=0.2)
    calls = {"n": 0}

    class Usage:
        def __init__(self, free):
            self.free = free

    def disk_usage(_path):
        calls["n"] += 1
        return Usage(10e9 if calls["n"] <= 1 else 100e6)  # second volume: below the 512 MB reserve

    w = ExperimentWriter(tmp_path, disk_usage=disk_usage)
    _r, res = run(sim, p, tmp_path, writer=w)
    assert res.state is MDAState.PARTIAL and "disk nearly full" in res.message
    rd = ExperimentReader(Path(res.folder))
    assert rd.manifest["volumes"][0]["status"] == "completed"
    assert rd.manifest["volumes"][1]["status"] == "not acquired"
    assert res.images_written == p.n_c * p.n_z


def test_d004_failed_volume_write_leaves_no_temp_folder_marked_complete(sim, tmp_path, monkeypatch):
    from openevos.imaging import mda_store

    p = simplan(sim, n_t=2, n_p=1, interval=0.2)
    real = mda_store.write_zstack_folder
    n = {"i": 0}

    def flaky(out, zset, **kw):
        n["i"] += 1
        if n["i"] == 2:
            (out / "stack.ome.tif").write_bytes(b"half")
            raise OSError("device error")
        return real(out, zset, **kw)

    monkeypatch.setattr(mda_store, "write_zstack_folder", flaky)
    _r, res = run(sim, p, tmp_path)
    assert res.state is MDAState.PARTIAL and "writing failed" in res.message
    folder = Path(res.folder)
    assert not list(folder.rglob(".*.tmp"))
    assert not (folder / "Positions/P0001_Here/T000002").exists()
    assert ExperimentReader(folder).manifest["volumes"][1]["status"] == "not acquired"


def test_storage_full_raised_before_writing():
    w = ExperimentWriter(Path("/tmp"), disk_usage=lambda _p: type("U", (), {"free": 1e6})())
    w.folder = Path("/tmp")
    with pytest.raises(StorageFull):
        w.check_space(10)


# ====================================================================================== pre-flight and gates
def test_s001_planes_outside_focus_travel_block_before_any_move(sim, tmp_path):
    _lo, hi = sim.controller.axis_limits(sim.profile.focus_device, "z")
    p = simplan(sim)
    far = replace(p, global_anchor_z_um=hi - 0.2, z=replace(p.z, above_um=50.0))
    pre = preflight(far, sim, save_root=tmp_path)
    assert not pre.ok and any("focus" in e or "limit" in e or "travel" in e for e in pre.errors), pre.errors


def test_s004_no_focus_telemetry_blocks(sim, tmp_path, monkeypatch):
    p = simplan(sim)
    monkeypatch.setattr(sim.controller, "focus_z", lambda: None)
    pre = preflight(p, sim, save_root=tmp_path)
    assert not pre.ok


def test_physical_positions_are_gated_until_xy_qualification(sim, tmp_path, monkeypatch):
    p = simplan(sim, n_p=2)
    assert preflight(p, sim, save_root=tmp_path).ok  # the simulator runs positions
    monkeypatch.setattr(type(sim), "simulated", property(lambda _s: False), raising=False)
    pre = preflight(p, sim, save_root=tmp_path)
    assert not pre.ok and any("qualif" in e for e in pre.errors), pre.errors
    # no XY move (one position where the stage is): Z x C x T stays available
    assert not any("qualif" in e for e in preflight(simplan(sim), sim, save_root=tmp_path).errors)


def test_storage_preflight_blocks_when_space_is_short(sim, tmp_path, monkeypatch):
    monkeypatch.setattr(mda, "_free_mb", lambda _f: 100.0)
    pre = preflight(simplan(sim, n_t=3), sim, save_root=tmp_path)
    assert not pre.ok and any("space" in e or "free" in e or "save" in e for e in pre.errors)


# ====================================================================================== page
@pytest.fixture(scope="module")
def app():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from openevos.ui import theme

    a = QApplication.instance() or QApplication([])
    theme.apply_theme("dark", a)
    return a


@pytest.fixture
def window(app, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    from openevos.ui.session_model import SessionModel
    from openevos.ui.shell import MainWindow

    s = build_sim_session(ThreadClock(), fast=True, fps=20, settings=AppSettings())
    w = MainWindow(SessionModel(s))
    w.resize(1440, 900)
    w.show()
    pump(app, 0.5)
    w.navigate("multid")
    pump(app, 0.3)
    yield w
    w.model.stop_all()
    pump(app, 0.3)
    w.close()
    s.shutdown()


def pump(app, t):
    end = time.monotonic() + t
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


def test_u003_dimensions_toggle_recompute_counts_and_preview(app, window, tmp_path):
    pg = window.pages["multid"]
    pg.md.save_folder = str(tmp_path / "exp")
    pg.preset.setCurrentIndex(pg.preset.findData("zc"))
    pg._apply_preset()
    pg.refresh()
    n0 = pg.last_summary.images
    assert pg.last_summary.n_t == 1 and pg.events.count() >= 1
    pg.card_t.switch.setChecked(True)
    pg.t_count.setValue(4)
    pg.refresh()
    assert pg.last_summary.images == 4 * n0
    assert pg.preset.currentData() == "zct"
    pg.card_z.switch.setChecked(False)
    pg.refresh()
    assert pg.last_summary.n_z == 1 and pg.card_z.summary.text() == "one plane"


def test_page_runs_monitors_and_opens_review(app, window, tmp_path):
    pg = window.pages["multid"]
    pg.md.save_folder = str(tmp_path / "exp")
    pg.preset.setCurrentIndex(pg.preset.findData("zcp"))
    pg._apply_preset()
    pg.z_a.setValue(1)
    pg.z_b.setValue(1)
    pg.add_current_position()
    pg.add_current_position()
    pg.positions[1] = replace(pg.positions[1], x=pg.positions[1].x + 500)
    pg._fill_table()
    pg.refresh()
    assert pg.btn_acquire.isEnabled(), pg.last_preflight
    total = pg.last_summary.images
    assert pg.acquire(ask=lambda _q: True)
    assert pg.stack.currentIndex() == 1 and pg.btn_stop.isEnabled()
    t0 = time.monotonic()
    while window.model.tasks.busy and time.monotonic() - t0 < 60:
        pump(app, 0.1)
    pump(app, 0.3)
    assert pg._reader is not None and pg._reader.manifest["images_written"] == total
    assert pg.m_progress.value() == total and "Completed" in pg.m_status.text()
    assert pg.r_p.count() == 2 and pg.r_view.has_image()
    pg.r_mode.setCurrentIndex(pg.r_mode.findData("max"))
    pump(app, 0.2)
    assert "Maximum" in pg.r_zlab.text() and pg.r_view.has_image()
    assert pg.r_missing.item(0).text().startswith("Every planned volume")


def test_page_plan_file_round_trip_and_gate_note(app, window, tmp_path, monkeypatch):
    pg = window.pages["multid"]
    pg.card_p.switch.setChecked(True)
    pg.add_current_position()
    pg.name.setText("Saved")
    plan = pg.plan()
    f = tmp_path / "plan.json"
    f.write_text(plan.to_json())
    pg.name.setText("changed")
    pg.positions.clear()
    assert pg.load_plan_file(f)
    assert pg.name.text() == "Saved" and len(pg.positions) == 1
    assert not pg.load_plan_file(tmp_path / "missing.json")
    monkeypatch.setattr(type(pg.session), "simulated", property(lambda _s: False), raising=False)
    pg.refresh()
    assert pg.lbl_gate.isVisibleTo(pg) and "qualified" in pg.lbl_gate.text()
    assert not pg.btn_acquire.isEnabled()


def test_u001_monitor_fits_1280_by_760(app, window):
    window.resize(1280, 760)
    pg = window.pages["multid"]
    pg._go_step("acquire")
    pump(app, 0.4)
    for w in (pg.m_view, pg.m_status, pg.btn_stop, pg.btn_pause):
        r = w.rect().translated(w.mapTo(window, w.rect().topLeft()))
        assert window.rect().contains(r.center()), w.accessibleName() or w.text()
    pg._go_step("plan")
    pump(app, 0.3)
    r = pg.btn_acquire.rect().translated(pg.btn_acquire.mapTo(window, pg.btn_acquire.rect().topLeft()))
    assert window.rect().contains(r.bottomRight())


def test_frames_are_never_silently_dropped_counts_match(sim, tmp_path):
    """D002 (no silent loss): every image the runner reports is journalled and in a file."""
    p = simplan(sim, n_t=1, n_p=2)
    got: list = []
    _r, res = run(sim, p, tmp_path, on_image=lambda *a: got.append(a[3]))
    rd = ExperimentReader(Path(res.folder))
    n_file = sum(rd.load(0, i)["stacks"][c][0].shape[0] for i in range(2) for c in ("cube1", "cube2"))
    assert len(got) == res.images_written == n_file == p.n_events
    assert np.all(np.diff(got) == 1)


def test_page_map_clicks_add_and_remove_wells_and_chips_count(app, window, tmp_path):  # kit mockup 08, Plan
    pg = window.pages["multid"]
    pg.md.save_folder = str(tmp_path / "exp")
    pg.preset.setCurrentIndex(pg.preset.findData("zc"))
    pg._apply_preset()
    pg.refresh()
    assert not pg.card_p.enabled() and not pg.chip["p"].on
    first = window.session.vessel.regions[0].region_id
    second = window.session.vessel.regions[1].region_id
    pg._map_clicked(first)  # a well on the map: P switches on, one position at the well's centre
    pg._map_clicked(second)
    pg.refresh()
    assert pg.card_p.enabled() and [p.label for p in pg.positions] == [first, second]
    assert pg.positions[0].moves and pg.positions[0].position_id == "P0001"
    assert pg.pmap.selected == {first, second} and pg.chip["p"].on and pg.chip["p"].count == 2
    assert pg.chip["z"].count == pg.last_summary.n_z and pg.chip["c"].count == pg.last_summary.n_c
    assert pg.kv_sum.values["images"].text() == f"{pg.last_summary.images:,}"
    assert "Selected 2 of" in pg.lbl_pmap.text()
    pg._map_clicked(first)  # again: removed, the rest renumbered
    pg.refresh()
    assert [p.label for p in pg.positions] == [second] and pg.positions[0].position_id == "P0001"
    pg._clear_positions()
    pg.refresh()
    assert not pg.positions and pg.chip["p"].count == 0 and not pg.btn_acquire.isEnabled()  # P on, none listed


def test_page_monitor_axes_queue_and_review_tree(app, window, tmp_path):  # kit doc 06 §3, §4
    pg = window.pages["multid"]
    pg.md.save_folder = str(tmp_path / "exp")
    pg.preset.setCurrentIndex(pg.preset.findData("zcp"))
    pg._apply_preset()
    pg.z_a.setValue(1)
    pg.z_b.setValue(1)
    pg.add_current_position()
    pg.add_current_position()
    pg.positions[1] = replace(pg.positions[1], x=pg.positions[1].x + 500)
    pg._fill_table()
    pg.refresh()
    summ = pg.last_summary
    assert pg.acquire(ask=lambda _q: True)
    t0 = time.monotonic()
    while window.model.tasks.busy and time.monotonic() - t0 < 60:
        pump(app, 0.1)
    pump(app, 0.3)
    assert pg.m_queue.rowCount() == summ.n_p
    assert all(pg.m_queue.item(r, 4).text() == "Done" for r in range(summ.n_p))
    assert pg.ax["z"].total == summ.n_z and pg.ax["p"].total == summ.n_p
    assert pg.kv_run.values["images"].text().startswith(f"{summ.images:,} / {summ.images:,}")
    tree = pg.r_tree
    assert tree.topLevelItemCount() == summ.n_p
    assert tree.topLevelItem(0).childCount() == summ.n_t
    assert tree.topLevelItem(0).child(0).text(2) == "completed"
    pg._tree_clicked(tree.topLevelItem(1).child(0))
    pump(app, 0.2)
    assert pg.r_p.currentIndex() == 1 and pg.r_view.has_image()
    keys = [pg.r_meta.item(r, 0).text() for r in range(pg.r_meta.rowCount())]
    assert {"Dataset", "Position", "Stage", "Exposure"} <= set(keys)
    assert pg.btn_rfolder.isEnabled() and pg.btn_rmanifest.isEnabled()
