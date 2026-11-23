"""Configuration-driven instrument assembly (plan §6.1 rule 2: backend selection is recorded).

    InstrumentConfig ──▶ backend factory (by name) ──▶ DeviceRegistry (HAL devices)
                                                   ──▶ command core (build_core)
                                                         each device ─▶ HalDeviceBackend ─▶ DeviceManager
                                                         instrument interlocks ─▶ SafetyManager
                                                   ──▶ CoreGateway (command port) ──▶ MicroscopeController

Every state change the UI makes goes through `core.router` (ADR-0007, ADR-0008). The UI asks for
an `Instrument` by configuration; it never imports a backend module.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openevos.backends import BACKENDS, TELEMETRY
from openevos.core.application import CoreServices, build_core
from openevos.core.audit import AuditTrail
from openevos.core.clock import ThreadClock
from openevos.core.command_language import DEFAULT_LIMITS, POSITION_TOLERANCE_UM, OperationalMode
from openevos.core.controller import MicroscopeController
from openevos.core.core_gateway import CoreGateway
from openevos.core.device_manager import OwnershipRegistry
from openevos.hal.adapter import HalDeviceBackend
from openevos.hal.device_registry import DeviceRegistry
from openevos.hal.interfaces import IlluminationDevice
from openevos.hal.types import Role
from openevos.safety.interlocks import InstrumentInterlocks


@dataclass
class InstrumentConfig:
    backend: str = "simulation"
    mode: OperationalMode = OperationalMode.SIMULATION
    backend_options: dict[str, Any] = field(default_factory=dict)
    limits: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_LIMITS))
    audit_path: Path | None = None
    config_ref: str = "simulation-default"
    #: optional read-only telemetry sources (ADR-0023), e.g. {"technosoft": {"port": "COM3"}}
    telemetry: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class Instrument:
    config: InstrumentConfig
    registry: DeviceRegistry
    gateway: CoreGateway
    audit: AuditTrail
    controller: MicroscopeController
    core: CoreServices
    telemetry: list[Any] = field(default_factory=list)

    def close(self) -> None:
        try:
            for svc in self.telemetry:
                svc.stop()
            self.controller.shutdown()
        finally:
            self.core.shutdown()
            stop = getattr(self.core.clock, "stop", None)
            if stop is not None:
                stop()


class BackendUnavailable(RuntimeError):
    pass


def load_backend(name: str) -> Any:
    target = BACKENDS.get(name)
    if target is None:
        raise BackendUnavailable(f"backend '{name}' is not available (known: {sorted(BACKENDS)})")
    module_name, _, attr = target.partition(":")
    return getattr(importlib.import_module(module_name), attr)


def load_telemetry(name: str) -> Any:
    target = TELEMETRY.get(name)
    if target is None:
        raise BackendUnavailable(f"telemetry source '{name}' is not available (known: {sorted(TELEMETRY)})")
    module_name, _, attr = target.partition(":")
    return getattr(importlib.import_module(module_name), attr)


def attach_telemetry(core: CoreServices, sources: dict[str, dict[str, Any]]) -> list[Any]:
    """Register read-only telemetry devices (native PHYSICAL backends with no commands, ADR-0023)."""
    services = []
    for name, options in sources.items():
        svc = load_telemetry(name)(dict(options))
        for backend in svc.backends:
            core.devices.register(backend, backend.capabilities())
        services.append(svc)
    return services


def attach_registry(core: CoreServices, registry: DeviceRegistry, limits: dict[str, float]) -> None:
    """Register every HAL device with the core (device_id = role value) and install the
    instrument's cross-device interlocks."""
    for role, dev in registry.items():
        backend = HalDeviceBackend(role.value, dev, core.clock)
        core.devices.register(backend, backend.capabilities())
    illum = registry.get(Role.ILLUMINATION)
    slots = tuple((c.channel_id, c.carriage_slot) for c in illum.get_channels()) \
        if isinstance(illum, IlluminationDevice) else ()
    core.safety.add_interlock(InstrumentInterlocks(
        focus_id=Role.FOCUS.value if Role.FOCUS in registry else None,
        objective_turret_id=Role.OBJECTIVE_TURRET.value if Role.OBJECTIVE_TURRET in registry else None,
        illumination_id=Role.ILLUMINATION.value if illum is not None else None,
        turret_safe_z=limits.get("turret_safe_z_um"), position_tolerance=POSITION_TOLERANCE_UM,
        channel_slots=slots))


def build_instrument(config: InstrumentConfig | None = None, *, initialize: bool = True,
                     channel: str | None = None) -> Instrument:
    cfg = config or InstrumentConfig()
    factory = load_backend(cfg.backend)
    registry: DeviceRegistry = factory(dict(cfg.backend_options))
    limits = dict(DEFAULT_LIMITS)
    limits.update(cfg.limits)
    # one ownership registry per instrument: these simulated devices belong to this instance only
    core = build_core(ThreadClock(), ownership=OwnershipRegistry())
    telemetry: list[Any] = []
    try:
        attach_registry(core, registry, limits)
        telemetry = attach_telemetry(core, cfg.telemetry)
        audit = AuditTrail(cfg.audit_path)
        gateway = CoreGateway(core, registry, mode=cfg.mode, limits=limits, audit=audit,
                              config_ref=cfg.config_ref)
        gateway.note("backend.selected", details={
            "backend": cfg.backend, "mode": cfg.mode.value, "execution": "command core",
            "options": {k: str(v) for k, v in cfg.backend_options.items()},
            "devices": {r.value: d.identify().display_name for r, d in registry.items()},
            "telemetry": {name: {k: str(v) for k, v in opts.items()} for name, opts in cfg.telemetry.items()},
        })
        gateway.connect_all()
        for svc in telemetry:
            if getattr(svc, "motion", False):  # ADR-0024: the session opted in to X/Y stage control
                gateway.motor_control_device = "motor.stage"
            svc.start()
        controller = MicroscopeController(gateway)
        if initialize:
            controller.initialize(channel)
    except Exception:
        for svc in telemetry:
            svc.stop()
        core.shutdown()
        core.clock.stop()  # type: ignore[attr-defined]
        raise
    return Instrument(cfg, registry, gateway, audit, controller, core, telemetry)
