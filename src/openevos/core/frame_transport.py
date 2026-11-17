"""Frame transport: preview and scientific paths, kept separate (handoff §14).

Camera frame ──► Frame.adopt (copy if the SDK reuses its buffer; then read-only)
                   ├──► PreviewCache   bounded, may drop stale frames, counts drops
                   └──► AcquisitionStream (only if the frame belongs to an active acquisition)
                          bounded queue ──► dedicated writer thread ──► FrameSink

Scientific-path guarantees:
* The producer never waits on disk: `submit()` is non-blocking.
* No silent loss. If the queue is full or the sink fails, the stream FAULTS, publishes a
  StorageFaultEvent with the acquisition ID and frame index, and refuses further frames, so the
  acquisition must stop. Every frame is accounted for as written, failed or rejected at ingress.
* Frames must arrive in strictly increasing frame_index order for their acquisition.
* `finish()` flushes outstanding frames, or faults explicitly if they cannot be written in time.
"""

from __future__ import annotations

import json
import queue
import threading
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Protocol

import numpy as np

from openevos.core.event_bus import AcquisitionStatusEvent, EventBus, StorageFaultEvent
from openevos.core.models import utcnow


@dataclass(frozen=True)
class FrameMetadata:
    device_id: str
    frame_index: int
    synthetic: bool
    acquisition_id: str | None = None
    timestamp: datetime = field(default_factory=utcnow)
    channel: str | None = None
    exposure_ms: float | None = None
    extra: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, eq=False)
class Frame:
    data: np.ndarray
    metadata: FrameMetadata

    @classmethod
    def adopt(cls, array: np.ndarray, metadata: FrameMetadata, *, owned: bool) -> Frame:
        """Take ownership. If the producer may reuse `array` (owned=False), copy it first.
        The stored array is read-only, so preview and storage can share it safely."""
        data = array if owned else np.array(array, copy=True)
        if not data.flags.c_contiguous:
            data = np.ascontiguousarray(data)
        data.flags.writeable = False
        return cls(data, metadata)


class PreviewCache:
    """Latest-N frames for display. Dropping here is expected and only counted."""

    def __init__(self, capacity: int = 1):
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self._buf: deque[Frame] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._received = 0
        self._dropped = 0

    def put(self, frame: Frame) -> None:
        with self._lock:
            if len(self._buf) == self._buf.maxlen:
                self._dropped += 1
            self._buf.append(frame)
            self._received += 1

    def latest(self) -> Frame | None:
        with self._lock:
            return self._buf[-1] if self._buf else None

    def take_latest(self) -> Frame | None:
        """Pop the newest frame for rendering; older pending frames count as dropped."""
        with self._lock:
            if not self._buf:
                return None
            frame = self._buf.pop()
            self._dropped += len(self._buf)
            self._buf.clear()
            return frame

    @property
    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"received": self._received, "dropped": self._dropped}


class FrameSink(Protocol):
    """Durable writer. Called only from the stream's writer thread."""

    def open(self, acquisition_id: str) -> None: ...
    def write(self, frame: Frame) -> None: ...
    def close(self) -> None: ...


class StreamState(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"
    FAULTED = "faulted"
    ABORTED = "aborted"


@dataclass(frozen=True)
class AcquisitionSummary:
    acquisition_id: str
    state: StreamState
    accepted: int
    written: int
    failed: int
    rejected_at_ingress: int
    expected: int | None
    message: str | None

    @property
    def reconciled(self) -> bool:
        """Every accepted frame is accounted for as written or failed."""
        return self.accepted == self.written + self.failed

    @property
    def complete(self) -> bool:
        return (self.state is StreamState.COMPLETED and self.failed == 0 and self.rejected_at_ingress == 0
                and (self.expected is None or self.written == self.expected))


_STOP = object()


class AcquisitionStream:
    def __init__(self, acquisition_id: str, sink: FrameSink, bus: EventBus, *, capacity: int = 64,
                 expected_frames: int | None = None):
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self.acquisition_id, self._sink, self._bus = acquisition_id, sink, bus
        self.expected = expected_frames
        self._q: queue.Queue = queue.Queue(maxsize=capacity)
        self._lock = threading.Lock()
        self._state = StreamState.CREATED
        self._message: str | None = None
        self._last_index = -1
        self._stop_sent = False
        self.accepted = self.written = self.failed = self.rejected = 0
        self._thread = threading.Thread(target=self._writer, name=f"openevos-writer-{acquisition_id}", daemon=True)

    @property
    def state(self) -> StreamState:
        return self._state

    @property
    def accepting(self) -> bool:
        return self._state is StreamState.RUNNING

    def start(self) -> None:
        with self._lock:
            if self._state is not StreamState.CREATED:
                raise RuntimeError("stream already started")
            self._sink.open(self.acquisition_id)  # fail fast before any frame is produced
            self._state = StreamState.RUNNING
        self._thread.start()
        self._status("started")

    def submit(self, frame: Frame) -> bool:
        """Non-blocking. Returns False if the frame was not accepted; the stream is then faulted
        (or already closed) and the producer must stop the acquisition."""
        md = frame.metadata
        with self._lock:
            if self._state is not StreamState.RUNNING:
                self.rejected += 1
                return False
            if md.acquisition_id != self.acquisition_id:
                self.rejected += 1
                self._fault_locked(f"frame from acquisition {md.acquisition_id!r} sent to {self.acquisition_id!r}", md.frame_index)
                return False
            if md.frame_index <= self._last_index:
                self.rejected += 1
                self._fault_locked(f"frame index {md.frame_index} out of order (last {self._last_index})", md.frame_index)
                return False
            try:
                self._q.put_nowait(frame)
            except queue.Full:
                self.rejected += 1
                self._fault_locked(f"storage back-pressure: writer queue full ({self._q.maxsize}) at frame {md.frame_index}",
                                   md.frame_index)
                return False
            self._last_index = md.frame_index
            self.accepted += 1
            return True

    def finish(self, timeout: float = 30.0) -> AcquisitionSummary:
        """Stop accepting, flush the queue, close the sink. Faults explicitly on timeout."""
        with self._lock:
            if self._state is StreamState.RUNNING:
                self._state = StreamState.COMPLETED  # provisional; writer errors can still fault it
            send_stop = self._thread.ident is not None and not self._stop_sent
            self._stop_sent = self._stop_sent or send_stop
        if send_stop:
            try:
                self._q.put(_STOP, timeout=timeout)
            except queue.Full:
                with self._lock:
                    self._fault_locked(f"writer stalled; could not flush within {timeout}s", None)
            self._thread.join(timeout)
            if self._thread.is_alive():
                with self._lock:
                    self._fault_locked(f"writer did not flush within {timeout}s", None)
        return self.summary()

    def abort(self, reason: str) -> AcquisitionSummary:
        with self._lock:
            if self._state in (StreamState.RUNNING, StreamState.CREATED):
                self._state = StreamState.ABORTED
                self._message = reason
        return self.finish()

    def summary(self) -> AcquisitionSummary:
        with self._lock:
            return AcquisitionSummary(self.acquisition_id, self._state, self.accepted, self.written, self.failed,
                                      self.rejected, self.expected, self._message)

    # ------------------------------------------------------------------ writer thread
    def _writer(self) -> None:
        while True:
            item = self._q.get()
            if item is _STOP:
                break
            frame: Frame = item
            with self._lock:
                faulted = self._state is StreamState.FAULTED
            if faulted:
                with self._lock:
                    self.failed += 1  # accounted, not silently dropped
                continue
            try:
                self._sink.write(frame)
                with self._lock:
                    self.written += 1
            except Exception as exc:  # noqa: BLE001
                with self._lock:
                    self.failed += 1
                    self._fault_locked(f"sink write failed: {exc.__class__.__name__}: {exc}", frame.metadata.frame_index)
        # drain anything left after a fault-triggered stop
        while True:
            try:
                item = self._q.get_nowait()
            except queue.Empty:
                break
            if item is not _STOP:
                with self._lock:
                    self.failed += 1
        try:
            self._sink.close()
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._fault_locked(f"sink close failed: {exc}", None)
        s = self.summary()
        self._status(s.state.value if s.state is not StreamState.RUNNING else "completed")

    def _fault_locked(self, message: str, frame_index: int | None) -> None:
        first = self._state is not StreamState.FAULTED
        self._state = StreamState.FAULTED
        if first:
            self._message = message
            self._bus.publish(StorageFaultEvent(self.acquisition_id, message, frame_index))

    def _status(self, state: str) -> None:
        s = self.summary()
        self._bus.publish(AcquisitionStatusEvent(self.acquisition_id, state, s.accepted, s.written, s.failed,
                                                 s.rejected_at_ingress, s.expected, s.message))


class FrameTransport:
    """Ingress point for camera frames: adopt once, then route to preview and/or storage."""

    def __init__(self, bus: EventBus, preview: PreviewCache | None = None):
        self.bus = bus
        self.preview = preview or PreviewCache(1)
        self._streams: dict[str, AcquisitionStream] = {}
        self._lock = threading.Lock()

    def open_stream(self, acquisition_id: str, sink: FrameSink, *, capacity: int = 64,
                    expected_frames: int | None = None) -> AcquisitionStream:
        with self._lock:
            if acquisition_id in self._streams:
                raise ValueError(f"acquisition {acquisition_id} already open")
            stream = AcquisitionStream(acquisition_id, sink, self.bus, capacity=capacity, expected_frames=expected_frames)
            self._streams[acquisition_id] = stream
        stream.start()
        return stream

    def close_stream(self, acquisition_id: str, timeout: float = 30.0) -> AcquisitionSummary:
        with self._lock:
            stream = self._streams.pop(acquisition_id)
        return stream.finish(timeout)

    def push(self, array: np.ndarray, metadata: FrameMetadata, *, owned: bool) -> bool:
        """Returns False if the frame belongs to an acquisition that did not accept it."""
        frame = Frame.adopt(array, metadata, owned=owned)
        self.preview.put(frame)
        if metadata.acquisition_id is None:
            return True
        with self._lock:
            stream = self._streams.get(metadata.acquisition_id)
        if stream is None:
            self.bus.publish(StorageFaultEvent(metadata.acquisition_id, "no open stream for acquisition",
                                               metadata.frame_index))
            return False
        return stream.submit(frame)


# ------------------------------------------------------------------------------------ sinks
class MemorySink:
    """Keeps frames in memory (tests, small previews of a sequence)."""

    def __init__(self) -> None:
        self.frames: list[Frame] = []
        self.opened = self.closed = False

    def open(self, acquisition_id: str) -> None:
        self.opened = True

    def write(self, frame: Frame) -> None:
        self.frames.append(frame)

    def close(self) -> None:
        self.closed = True


class NpyDirectorySink:
    """Simple durable sink: one .npy per frame plus a JSON-lines metadata index.
    A stand-in until the OME-TIFF/Zarr writer (storage/image_writer.py, Phase 2.4)."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self._dir: Path | None = None
        self._index = None

    def open(self, acquisition_id: str) -> None:
        self._dir = self.root / acquisition_id
        self._dir.mkdir(parents=True, exist_ok=False)
        self._index = open(self._dir / "frames.jsonl", "a", encoding="utf-8")  # noqa: SIM115 — held open for the stream; closed in close()

    def write(self, frame: Frame) -> None:
        assert self._dir is not None and self._index is not None
        md = frame.metadata
        name = f"frame_{md.frame_index:06d}.npy"
        np.save(self._dir / name, frame.data, allow_pickle=False)
        rec = asdict(md) | {"file": name, "shape": list(frame.data.shape), "dtype": str(frame.data.dtype)}
        rec["timestamp"] = md.timestamp.isoformat()
        self._index.write(json.dumps(rec) + "\n")
        self._index.flush()

    def close(self) -> None:
        if self._index is not None:
            self._index.close()
