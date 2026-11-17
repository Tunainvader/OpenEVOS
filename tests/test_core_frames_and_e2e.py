"""Handoff §23.5 (frame transport), §24 (end-to-end acceptance), concurrency and shutdown."""

import json
import threading
import time

import numpy as np
import pytest
from core_helpers import Recorder, stage_move

from openevos.core.application import attach_simulated_instrument, build_core
from openevos.core.clock import ManualClock, ThreadClock
from openevos.core.command_scheduler import DeviceWorker
from openevos.core.device_manager import OwnershipError, OwnershipRegistry
from openevos.core.event_bus import (
    AcquisitionStatusEvent,
    EventBus,
    HardwareFaultEvent,
    StorageFaultEvent,
)
from openevos.core.frame_transport import (
    AcquisitionStream,
    Frame,
    FrameMetadata,
    FrameTransport,
    MemorySink,
    NpyDirectorySink,
    PreviewCache,
    StreamState,
)
from openevos.core.models import Activity, Connection
from openevos.core.models import CommandStatus as S
from openevos.safety.interlocks import claim_for_acquisition


def md(i, acq="acq"):
    return FrameMetadata(device_id="cam", frame_index=i, synthetic=True, acquisition_id=acq)


def arr(i):
    return np.full((4, 4), i, dtype=np.uint16)


class FailingSink(MemorySink):
    def __init__(self, fail_at):
        super().__init__()
        self.fail_at = fail_at
        self.threads = set()

    def write(self, frame):
        self.threads.add(threading.current_thread().name)
        if frame.metadata.frame_index == self.fail_at:
            raise OSError("disk full (simulated)")
        super().write(frame)


class BlockingSink(MemorySink):
    def __init__(self):
        super().__init__()
        self.gate = threading.Event()

    def write(self, frame):
        self.gate.wait(10)
        super().write(frame)


# ------------------------------------------------------------------ frames
def test_frame_ownership_copy_and_read_only():
    src = arr(1)
    f = Frame.adopt(src, md(0), owned=False)
    src[:] = 99  # SDK reuses its buffer
    assert int(f.data[0, 0]) == 1
    with pytest.raises(ValueError):
        f.data[0, 0] = 5
    own = arr(2)
    g = Frame.adopt(own, md(1), owned=True)
    assert g.data is own and not own.flags.writeable


def test_preview_is_bounded_and_counts_drops():
    p = PreviewCache(capacity=2)
    for i in range(100):
        p.put(Frame.adopt(arr(i), md(i, None), owned=True))
    assert p.latest().metadata.frame_index == 99 and p.stats == {"received": 100, "dropped": 98}
    assert p.take_latest().metadata.frame_index == 99 and p.latest() is None


def test_scientific_frames_preserved_in_order_off_the_producer_thread():
    bus = EventBus()
    sink = FailingSink(fail_at=-1)
    s = AcquisitionStream("acq", sink, bus, capacity=8, expected_frames=100)
    s.start()
    for i in range(100):
        while not s._q.empty() and s._q.qsize() >= 7:
            time.sleep(0.0005)  # test producer paces itself; the stream itself never blocks
        assert s.submit(Frame.adopt(arr(i), md(i), owned=True))
    summary = s.finish()
    assert summary.complete and summary.reconciled and summary.written == 100
    assert [f.metadata.frame_index for f in sink.frames] == list(range(100))
    assert all(int(f.data[0, 0]) == f.metadata.frame_index for f in sink.frames)  # metadata matches data
    assert sink.threads == {"openevos-writer-acq"} and sink.closed


def test_sink_failure_is_explicit_and_accounted():
    bus = EventBus()
    faults = []
    bus.subscribe(StorageFaultEvent, faults.append)
    s = AcquisitionStream("acq", FailingSink(fail_at=37), bus, capacity=1000)
    s.start()
    for i in range(60):
        s.submit(Frame.adopt(arr(i), md(i), owned=True))
    summary = s.finish()
    assert summary.state is StreamState.FAULTED and summary.reconciled and not summary.complete
    assert summary.written == 37 and faults[0].frame_index == 37 and faults[0].acquisition_id == "acq"
    assert not s.submit(Frame.adopt(arr(99), md(99), owned=True))


def test_queue_overflow_faults_instead_of_dropping():
    bus = EventBus()
    sink = BlockingSink()
    faults = []
    bus.subscribe(StorageFaultEvent, faults.append)
    s = AcquisitionStream("acq", sink, bus, capacity=4)
    s.start()
    results = [s.submit(Frame.adopt(arr(i), md(i), owned=True)) for i in range(20)]
    assert False in results and s.state is StreamState.FAULTED
    assert "back-pressure" in faults[0].message
    sink.gate.set()
    summary = s.finish()
    assert summary.reconciled and summary.rejected_at_ingress >= 1 and not summary.complete
    assert summary.accepted + summary.rejected_at_ingress == 20  # every frame accounted for


def test_order_and_identity_violations_fault():
    for bad in (md(0), md(5, acq="other")):
        s = AcquisitionStream("acq", MemorySink(), EventBus())
        s.start()
        assert s.submit(Frame.adopt(arr(1), md(1), owned=True)) or bad.acquisition_id == "other"
        assert not s.submit(Frame.adopt(arr(0), bad, owned=True))
        assert s.finish().state is StreamState.FAULTED


def test_finish_faults_if_writer_cannot_flush():
    sink = BlockingSink()
    s = AcquisitionStream("acq", sink, EventBus(), capacity=4)
    s.start()
    s.submit(Frame.adopt(arr(0), md(0), owned=True))
    summary = s.finish(timeout=0.2)
    assert summary.state is StreamState.FAULTED and "flush" in summary.message
    sink.gate.set()


def test_transport_routes_by_acquisition_and_reports_unknown():
    bus = EventBus()
    faults = []
    bus.subscribe(StorageFaultEvent, faults.append)
    t = FrameTransport(bus, PreviewCache(1))
    assert t.push(arr(0), md(0, None), owned=False)  # preview only
    assert not t.push(arr(0), md(0, "ghost"), owned=False) and faults[0].acquisition_id == "ghost"


def test_npy_directory_sink(tmp_path):
    s = AcquisitionStream("acq", NpyDirectorySink(tmp_path), EventBus())
    s.start()
    for i in range(3):
        s.submit(Frame.adopt(arr(i), md(i), owned=True))
    assert s.finish().complete
    lines = (tmp_path / "acq" / "frames.jsonl").read_text().splitlines()
    assert [json.loads(x)["frame_index"] for x in lines] == [0, 1, 2]
    assert int(np.load(tmp_path / "acq" / "frame_000002.npy")[0, 0]) == 2


# ------------------------------------------------------------------ concurrency
def test_simultaneous_conflicting_commands_only_one_reserves():
    clock = ManualClock()
    core = build_core(clock, worker_factory=DeviceWorker, ownership=OwnershipRegistry())
    attach_simulated_instrument(core, camera_size=(32, 32))
    n = 32
    barrier, tickets, lock = threading.Barrier(n), [], threading.Lock()

    def go(i):
        barrier.wait()
        t = core.router.submit(stage_move(100 + i, 0))
        with lock:
            tickets.append(t)

    ts = [threading.Thread(target=go, args=(i,)) for i in range(n)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    accepted = [t for t in tickets if t.status in (S.ACCEPTED, S.RUNNING)]
    rejected = [t for t in tickets if t.status is S.REJECTED]
    assert len(accepted) == 1 and len(rejected) == n - 1
    # later threads may see either the reservation or the already-observed motion; both fail closed
    assert all(("resource conflict" in t.record.message) or ("activity is moving" in t.record.message) for t in rejected)
    assert accepted[0].wait_for({S.RUNNING}, 2) is S.RUNNING
    clock.advance(1)
    assert accepted[0].wait(2) is S.SUCCEEDED
    core.shutdown()


def test_acquisition_reservation_interlocks_stage(manual_core):
    core, _sim, _clock, _rec = manual_core
    claim = claim_for_acquisition("sim-camera", ["sim-stage", "sim-focus"])
    assert core.router.reserve("acq-1", claim) == []
    t = core.router.submit(stage_move(10, 10))
    assert t.status is S.REJECTED and "must stay stable for acq-1" in t.record.message
    core.router.release("acq-1")
    t2 = core.router.submit(stage_move(10, 10))
    assert t2.status is S.ACCEPTED
    assert core.router.reserve("acq-2", claim)  # the moving stage now blocks a new acquisition


# ------------------------------------------------------------------ end-to-end acceptance (§24)
def test_e2e_simulated_stage_movement_realtime():
    clock = ThreadClock()
    reg = OwnershipRegistry()
    core = build_core(clock, worker_factory=DeviceWorker, ownership=reg)
    rec = Recorder(core.bus)
    attach_simulated_instrument(core, camera_size=(32, 32))
    t0 = time.perf_counter()
    t = core.router.submit(stage_move(600, 0))  # 0.3 s of simulated travel
    submit_latency = time.perf_counter() - t0
    assert submit_latency < 0.05  # caller (UI) is never blocked by motion
    assert t.wait(3) is S.SUCCEEDED
    tl = [x for x in rec.timeline if x[0] in ("cmd", "state:sim-stage")]
    idx = {v: i for i, v in enumerate(tl)}
    assert idx[("cmd", "accepted")] < idx[("state:sim-stage", "moving")] < idx[("cmd", "running")] < idx[("cmd", "succeeded")]
    assert core.store.get("sim-stage").activity is Activity.IDLE and core.resources.holders() == {}
    core.shutdown()
    clock.stop()
    assert not [th for th in threading.enumerate() if th.name.startswith("openevos-worker") and th.is_alive()]
    assert reg.owner_of("sim:sim-stage") is None


def test_e2e_100_frame_timelapse_saved(manual_core, tmp_path):
    core, sim, clock, _rec = manual_core
    statuses = []
    core.bus.subscribe(AcquisitionStatusEvent, statuses.append)
    stream = core.transport.open_stream("tl-001", NpyDirectorySink(tmp_path), expected_frames=100)
    sim.camera.start_stream(acquisition_id="tl-001", n_frames=100)
    assert core.store.get("sim-camera").activity is Activity.ACQUIRING
    # ManualClock releases due frames in one burst. Advance in steps of 10 frames and let the
    # writer drain in between, so the test checks the transport rather than the disk's speed
    # under a full test run (back-pressure faulting has its own test).
    import time as _time
    for _ in range(10):
        clock.advance(10 / 15)
        deadline = _time.monotonic() + 10.0
        while stream.written + stream.failed < stream.accepted and _time.monotonic() < deadline:
            _time.sleep(0.005)
    clock.advance(1.0)
    summary = core.transport.close_stream("tl-001")
    assert summary.complete and summary.written == 100, summary
    lines = [json.loads(x) for x in (tmp_path / "tl-001" / "frames.jsonl").read_text().splitlines()]
    assert [x["frame_index"] for x in lines] == list(range(100)) and all(x["synthetic"] for x in lines)
    stamps = [x["timestamp"] for x in lines]
    assert stamps == sorted(stamps)
    assert core.store.get("sim-camera").activity is Activity.IDLE
    assert core.transport.preview.stats["received"] == 100
    assert [s.state for s in statuses] == ["started", "completed"]
    assert stream.state is StreamState.COMPLETED


def test_e2e_storage_failure_stops_acquisition(manual_core):
    core, sim, clock, rec = manual_core
    sink = FailingSink(fail_at=50)
    stream = core.transport.open_stream("tl-002", sink, expected_frames=100)
    sim.camera.start_stream(acquisition_id="tl-002", n_frames=100)
    deadline = time.time() + 5
    while stream.state is not StreamState.FAULTED and time.time() < deadline:
        clock.advance(1 / 15)
        time.sleep(0.001)  # let the writer thread run (it is a real thread)
    clock.advance(1)  # next frame is refused -> camera stops
    produced = sim.camera.frames_produced
    clock.advance(5)
    assert not sim.camera.streaming and sim.camera.frames_produced == produced
    summary = core.transport.close_stream("tl-002")
    assert summary.state is StreamState.FAULTED and summary.reconciled and summary.written == 50
    assert any(e.frame_index == 50 for e in rec.of(StorageFaultEvent))


def test_e2e_camera_disconnect_reported(manual_core):
    core, sim, clock, rec = manual_core
    sim.camera.start_stream()
    clock.advance(0.5)
    sim.camera.simulate_unexpected_disconnect()
    snap = core.store.get("sim-camera")
    assert snap.connection is Connection.DISCONNECTED and not sim.camera.streaming
    assert any(e.device_id == "sim-camera" for e in rec.of(HardwareFaultEvent))


def test_single_owner_per_physical_device():
    reg = OwnershipRegistry()
    a = build_core(ManualClock(), ownership=reg)
    b = build_core(ManualClock(), ownership=reg)
    attach_simulated_instrument(a, camera_size=(32, 32), connect=False)
    with pytest.raises(OwnershipError):
        attach_simulated_instrument(b, camera_size=(32, 32), connect=False)
    a.shutdown()
    attach_simulated_instrument(b, camera_size=(32, 32), connect=False)  # released -> now allowed
    b.shutdown()


def test_core_has_no_qt_dependency_and_no_sleeps():
    import ast
    import subprocess
    import sys
    from pathlib import Path

    # Check in a fresh interpreter: in a combined suite the UI tests have already imported PySide6.
    probe = subprocess.run(
        [sys.executable, "-c", "import sys, openevos.core.application; print('PySide6' in sys.modules)"],
        capture_output=True, text=True, timeout=120, check=False,
    )
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == "False", "importing the core pulled in PySide6"
    root = Path(__file__).resolve().parents[1] / "src" / "openevos"
    # Only the clock implementations may turn simulated time into a real wait (ADR-0008). Every
    # other module must go through an injected clock object (e.g. `self._clock.sleep`), which
    # tests replace with a manual/fake clock that never blocks.
    clock_impls = {root / "core" / "clock.py", root / "backends" / "simulation" / "clock.py"}
    for sub in ("core", "backends", "safety", "hal"):
        for p in (root / sub).rglob("*.py"):
            tree = ast.parse(p.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                direct_sleep = (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                                and node.func.attr == "sleep" and isinstance(node.func.value, ast.Name)
                                and node.func.value.id == "time")
                imported_sleep = (isinstance(node, ast.ImportFrom) and node.module == "time"
                                  and any(a.name == "sleep" for a in node.names))
                if (direct_sleep or imported_sleep) and p not in clock_impls:
                    pytest.fail(f"{p.name}:{node.lineno} calls time.sleep; use the injected clock")
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    mods = [a.name for a in node.names] + ([node.module] if getattr(node, "module", None) else [])
                    assert not any(m and m.startswith(("PySide6", "PyQt")) for m in mods), p.name
