"""Read-only facade over a running session, for the MCP tools (ADR-0054).

Everything here reads: runtime snapshots from the state store, the command catalogue, the event
bus (by subscribing), the drift monitor's state, buffered captures and the latest preview frame.
Nothing here may change instrument state; `tests/test_mcp_link.py` enforces that by scanning the
source for state-changing calls.
"""

from __future__ import annotations

import io
import itertools
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import numpy as np

from openevos.core.event_bus import (
    AcquisitionStatusEvent,
    CommandLifecycleEvent,
    DeviceStateChanged,
    HardwareFaultEvent,
    StorageFaultEvent,
)

#: bounds that keep a single tool call cheap for the instrument and small for the model
MAX_PROBE_S = 10.0
MAX_PROBE_HZ = 50.0
MAX_EVENTS = 500
MAX_IMAGE_PX = 1024
EVENT_BUFFER = 2000
MONITOR_TAIL = 400


def _iso(t: datetime | None) -> str | None:
    return t.isoformat(timespec="milliseconds") if t else None


def _position(p) -> dict | None:
    if p is None:
        return None
    return {"axes": list(p.axes), "values": [round(v, 3) for v in p.values], "unit": p.unit, "frame": p.frame}


def _snapshot(s) -> dict:
    return {
        "device_id": s.device_id,
        "kind": s.kind.value,
        "backend": s.backend_kind.value,
        "connection": s.connection.value,
        "health": s.health.value,
        "activity": s.activity.value,
        "firmware_mode": s.firmware_mode.value,
        "recovery_required": s.recovery_required,
        "observed_position": _position(s.observed_position),
        "requested_position": _position(s.requested_position),
        "observed_at": _iso(s.observed_at),
        "note": s.note,
        "attributes": {k: (list(v) if isinstance(v, tuple) else v) for k, v in s.attributes},
    }


@dataclass(frozen=True)
class LoggedEvent:
    seq: int
    kind: str
    timestamp: str
    data: dict


class EventLog:
    """Bounded log of bus events worth showing an assistant: command lifecycle, faults, acquisition
    status and device *condition* changes (connection, health, recovery, firmware mode). Position
    updates are left out: `probe` samples those on request."""

    def __init__(self, bus, capacity: int = EVENT_BUFFER):
        self._events: deque[LoggedEvent] = deque(maxlen=capacity)
        self._seq = itertools.count(1)
        self._lock = threading.Lock()
        self._subs = [
            bus.subscribe(CommandLifecycleEvent, self._lifecycle),
            bus.subscribe(HardwareFaultEvent, self._fault),
            bus.subscribe(StorageFaultEvent, self._storage),
            bus.subscribe(AcquisitionStatusEvent, self._acquisition),
            bus.subscribe(DeviceStateChanged, self._device),
        ]

    def close(self) -> None:
        for sub in self._subs:
            sub.unsubscribe()
        self._subs.clear()

    def _add(self, kind: str, timestamp: datetime, data: dict) -> None:
        with self._lock:
            self._events.append(LoggedEvent(next(self._seq), kind, _iso(timestamp) or "", data))

    def _lifecycle(self, e: CommandLifecycleEvent) -> None:
        self._add("command", e.timestamp, {
            "command_id": e.command_id, "device_id": e.device_id, "status": e.status.value,
            "previous": e.previous_status.value if e.previous_status else None, "message": e.message,
            "result": _position(e.result), "correlation_id": e.correlation_id})

    def _fault(self, e: HardwareFaultEvent) -> None:
        self._add("fault", e.timestamp, {"device_id": e.device_id, "message": e.message,
                                         "severity": e.severity.value, "error_code": e.error_code,
                                         "command_id": e.command_id})

    def _storage(self, e: StorageFaultEvent) -> None:
        self._add("storage_fault", e.timestamp, {"acquisition_id": e.acquisition_id, "message": e.message,
                                                 "frame_index": e.frame_index})

    def _acquisition(self, e: AcquisitionStatusEvent) -> None:
        self._add("acquisition", e.timestamp, {
            "acquisition_id": e.acquisition_id, "state": e.state, "accepted": e.accepted,
            "written": e.written, "failed": e.failed, "expected": e.expected, "message": e.message})

    def _device(self, e: DeviceStateChanged) -> None:
        prev, cur = e.previous, e.current
        fields = ("connection", "health", "firmware_mode", "recovery_required")
        if prev is not None and all(getattr(prev, f) == getattr(cur, f) for f in fields):
            return  # position/activity only
        changed = {f: {"from": (getattr(prev, f).value if hasattr(getattr(prev, f), "value") else getattr(prev, f))
                       if prev else None,
                       "to": getattr(cur, f).value if hasattr(getattr(cur, f), "value") else getattr(cur, f)}
                   for f in fields if prev is None or getattr(prev, f) != getattr(cur, f)}
        self._add("device", e.timestamp, {"device_id": e.device_id, "changed": changed, "reason": e.reason})

    def since(self, seq: int = 0, limit: int = 100, kinds: set[str] | None = None) -> tuple[list[LoggedEvent], int]:
        limit = max(1, min(int(limit), MAX_EVENTS))
        with self._lock:
            rows = [e for e in self._events if e.seq > seq and (not kinds or e.kind in kinds)]
            last = self._events[-1].seq if self._events else 0
        return rows[-limit:], last


class InstrumentView:
    """What an assistant may see. Construct with a `Session`; call `close()` when done."""

    def __init__(self, session, *, clock=time.monotonic, sleep=time.sleep):
        self._s = session
        self._clock = clock
        self._sleep = sleep
        self.events = EventLog(session.core.bus)

    def close(self) -> None:
        self.events.close()

    # ------------------------------------------------------------------ state
    def state(self) -> dict[str, Any]:
        s = self._s
        ctl, prof = s.controller, s.profile
        light = ctl.light
        snaps = s.core.store.snapshot_all()
        xy = ctl.stage_xy()
        return {
            "simulated_optics": bool(getattr(s, "simulated", True)),
            "physical_stage": bool(s.physical_stage),
            "stage_device": prof.stage_device,
            "focus_device": prof.focus_device,
            "turret_device": prof.turret_device,
            "stage_xy_um": [round(v, 2) if v is not None else None for v in xy] if xy else None,
            "focus_z_um": round(ctl.focus_z(), 2) if ctl.focus_z() is not None else None,
            "objective_slot": ctl.objective_slot(),
            "light": {"channel": light.channel, "intensity_percent": light.intensity_percent,
                      "enabled": light.enabled},
            "vessel": {"id": s.vessel.vessel_id, "name": s.vessel.display_name,
                       "geometry_verified": s.vessel.geometry_verified},
            "stage_monitor": self.stage_monitor(tail=0)["monitors"],
            "devices": [_snapshot(v) for _, v in sorted(snaps.items())],
            "buffered_images": sorted(s.capture.buffers.all()),
        }

    def profile(self) -> dict[str, Any]:
        p = self._s.profile
        return {
            "cameras": [c.model_dump(mode="json") for c in p.cameras] if hasattr(p, "cameras") else [],
            "channels": [c.model_dump(mode="json") for c in p.channels],
            "objectives": [o.model_dump(mode="json") for o in p.objectives],
        }

    def command_catalog(self) -> list[dict[str, Any]]:
        """The instrument's named commands, for reference only: the AI actor may issue none of them."""
        from openevos.core.command_language import build_catalog

        out = []
        for spec in build_catalog().values():
            out.append({
                "name": spec.name, "summary": spec.summary, "risk": spec.risk.value,
                "requires_confirmation": spec.requires_confirmation, "safe_stop": spec.safe_stop,
                "params": [{"name": p.name, "type": p.kind.__name__, "unit": p.unit, "min": p.minimum,
                            "max": p.maximum, "device_range": p.device_range} for p in spec.params],
                "interlocks": list(spec.interlocks), "readback": spec.readback,
                "ai_may_issue": False,
            })
        return out

    # ------------------------------------------------------------------ telemetry
    def probe(self, device_ids: list[str] | None = None, duration_s: float = 2.0,
              rate_hz: float = 10.0) -> dict[str, Any]:
        """Sample observed positions and activity from the state store (no device I/O)."""
        duration_s = max(0.0, min(float(duration_s), MAX_PROBE_S))
        rate_hz = max(0.5, min(float(rate_hz), MAX_PROBE_HZ))
        store = self._s.core.store
        known = sorted(store.snapshot_all())
        ids = [d for d in (device_ids or known) if d in known]
        unknown = sorted(set(device_ids or []) - set(known))
        series: dict[str, list] = {d: [] for d in ids}
        period, t0 = 1.0 / rate_hz, self._clock()
        n = max(1, round(duration_s * rate_hz) + 1)
        for i in range(n):
            t = round(self._clock() - t0, 3)
            for d in ids:
                snap = store.get(d)
                if snap is None:
                    continue
                pos = snap.observed_position
                series[d].append([t, snap.activity.value, [round(v, 3) for v in pos.values] if pos else None])
            if i < n - 1:
                self._sleep(max(0.0, t0 + (i + 1) * period - self._clock()))
        axes = {}
        for d in ids:
            snap = store.get(d)
            if snap and snap.observed_position:
                axes[d] = {"axes": list(snap.observed_position.axes), "unit": snap.observed_position.unit}
        return {"columns": ["t_s", "activity", "values"], "axes": axes, "series": series,
                "unknown_devices": unknown, "duration_s": duration_s, "rate_hz": rate_hz}

    def stage_monitor(self, tail: int = 100) -> dict[str, Any]:
        """The X/Y drift monitor (ADR-0040) of each physical telemetry service, with recent samples."""
        tail = max(0, min(int(tail), MONITOR_TAIL))
        monitors = []
        for svc in getattr(self._s, "telemetry", []) or []:
            mon = getattr(svc, "monitor", None)
            if mon is None:
                continue
            entry = {"port": getattr(svc, "port_label", ""), **mon.state()}
            if tail:
                with mon._lock:  # the monitor's own lock: a consistent copy of its ring buffer
                    rows = list(mon.samples)[-tail:]
                entry["samples"] = {"columns": ["t_s", "x_apos", "y_apos", "x_cpos", "y_cpos", "x_moving",
                                                "y_moving"], "rows": [list(r) for r in rows]}
            monitors.append(entry)
        return {"monitors": monitors}

    # ------------------------------------------------------------------ images
    def images(self) -> list[dict[str, Any]]:
        out = []
        for slot, img in sorted(self._s.capture.buffers.all().items()):
            out.append(self._image_meta(img) | {"slot": slot})
        return out

    @staticmethod
    def _image_meta(img) -> dict[str, Any]:
        return {
            "channel": img.channel, "label": img.channel_label, "camera": img.camera.value,
            "shape": list(img.data.shape), "bit_depth": img.bit_depth, "synthetic": img.synthetic,
            "objective_slot": img.objective_slot, "magnification": img.magnification,
            "pixel_size_um": img.pixel_size_um,
            # false = nominal camera pixel / magnification, not measured: never use it for a µm measurement
            "pixel_size_calibrated": img.pixel_size_calibrated,
            "stage_xy_um": list(img.stage_xy_um) if img.stage_xy_um else None, "z_um": img.z_um,
            "exposure_ms": img.exposure_ms, "gain": img.gain, "led_percent": img.led_percent,
            "captured_at": _iso(img.captured_at),
        }

    def image_png(self, source: str = "live", max_px: int = 512) -> tuple[bytes, dict[str, Any]]:
        """A display-mapped PNG (linear 0.1-99.9 % stretch, downsampled) and its metadata.
        `source` is "live" (latest preview frame, not consumed) or a buffered channel slot."""
        max_px = max(64, min(int(max_px), MAX_IMAGE_PX))
        if source == "live":
            frame = self._s.core.transport.preview.latest()
            if frame is None:
                raise LookupError("no preview frame yet (live view not running)")
            arr = frame.data
            m = frame.metadata
            meta = {"source": "live", "device_id": m.device_id, "frame_index": m.frame_index,
                    "synthetic": m.synthetic, "channel": m.channel, "exposure_ms": m.exposure_ms,
                    "timestamp": _iso(m.timestamp), "shape": list(arr.shape)}
        else:
            img = self._s.capture.buffers.get(source)
            if img is None:
                raise LookupError(f"no buffered image in slot {source!r}; buffered: "
                                  f"{sorted(self._s.capture.buffers.all())}")
            arr = img.data
            meta = self._image_meta(img) | {"source": source}
        png, mapping = _to_png(arr, max_px)
        meta["display"] = mapping
        return png, meta


def _to_png(arr: np.ndarray, max_px: int) -> tuple[bytes, dict]:
    from PIL import Image  # imaging extra

    a = np.asarray(arr)
    step = max(1, int(np.ceil(max(a.shape[0], a.shape[1]) / max_px)))
    small = a[::step, ::step]
    data = small.astype(np.float64)
    lo, hi = (float(v) for v in np.percentile(data, (0.1, 99.9)))
    if hi <= lo:
        hi = lo + 1.0
    out = np.clip((data - lo) / (hi - lo) * 255.0, 0, 255).astype(np.uint8)
    mode = "RGB" if out.ndim == 3 and out.shape[2] == 3 else "L"
    if out.ndim == 3 and mode == "L":
        out = out[..., 0]
    buf = io.BytesIO()
    Image.fromarray(out, mode=mode).save(buf, format="PNG")
    return buf.getvalue(), {"mapping": "linear", "low": lo, "high": hi, "downsample": step,
                            "note": "display only; raw data unchanged"}
