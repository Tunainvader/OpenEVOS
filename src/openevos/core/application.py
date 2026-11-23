"""Composition root: builds one application-scoped set of core services (no globals).

    core = build_core(ThreadClock())                  # application
    core = build_core(ManualClock(), worker_factory=ManualWorker, ownership=OwnershipRegistry())  # tests
    sim = attach_simulated_instrument(core)

The UI (Phase 2.3) receives `core.bus` and subscribes with a Qt queued dispatcher; it submits
commands only through `core.router.submit()`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from openevos.core.clock import Clock
from openevos.core.command_router import CommandRouter
from openevos.core.command_scheduler import DeviceWorker, ResourceTable, Worker
from openevos.core.device_manager import PROCESS_OWNERSHIP, DeviceManager, OwnershipRegistry
from openevos.core.event_bus import EventBus
from openevos.core.frame_transport import FrameTransport, PreviewCache
from openevos.core.runtime_state_store import RuntimeStateStore
from openevos.safety.command_validator import SafetyManager


@dataclass
class CoreServices:
    clock: Clock
    bus: EventBus
    store: RuntimeStateStore
    devices: DeviceManager
    resources: ResourceTable
    safety: SafetyManager
    router: CommandRouter
    transport: FrameTransport

    def shutdown(self) -> None:
        self.devices.shutdown()


def build_core(clock: Clock, *, worker_factory: Callable[[str], Worker] = DeviceWorker,
               ownership: OwnershipRegistry = PROCESS_OWNERSHIP, preview_capacity: int = 1) -> CoreServices:
    bus = EventBus()
    store = RuntimeStateStore(bus)
    devices = DeviceManager(store, bus, ownership=ownership, worker_factory=worker_factory)
    resources = ResourceTable()
    safety = SafetyManager()
    router = CommandRouter(devices, safety, resources, bus, store, clock)
    transport = FrameTransport(bus, PreviewCache(preview_capacity))
    return CoreServices(clock, bus, store, devices, resources, safety, router, transport)


@dataclass
class SimulatedInstrument:
    stage: object
    focus: object
    camera: object


def attach_simulated_instrument(core: CoreServices, *, camera_size: tuple[int, int] = (640, 480),
                                fps: float = 15.0, connect: bool = True) -> SimulatedInstrument:
    from openevos.backends.simulation.minimal.camera import SimCamera
    from openevos.backends.simulation.minimal.motion import (
        SIM_XY_LIMITS,
        SIM_Z_LIMITS,
        sim_focus,
        sim_xy_stage,
    )

    stage = sim_xy_stage("sim-stage", core.clock)
    focus = sim_focus("sim-focus", core.clock)
    cam = SimCamera("sim-camera", core.clock, core.transport, width=camera_size[0], height=camera_size[1], fps=fps)
    core.devices.register(stage, stage.capabilities(SIM_XY_LIMITS))
    core.devices.register(focus, focus.capabilities(SIM_Z_LIMITS))
    core.devices.register(cam, cam.capabilities())
    if connect:
        for d in ("sim-stage", "sim-focus", "sim-camera"):
            core.devices.connect(d)
    return SimulatedInstrument(stage, focus, cam)
