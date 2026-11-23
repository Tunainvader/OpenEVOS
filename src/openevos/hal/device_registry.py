"""Device registry: one device object per instrument role, type-checked at registration."""

from __future__ import annotations

from collections.abc import Iterator

from openevos.hal.interfaces import ROLE_INTERFACE, HardwareDevice
from openevos.hal.types import Role


class RegistryError(KeyError):
    pass


class DeviceRegistry:
    def __init__(self) -> None:
        self._devices: dict[Role, HardwareDevice] = {}

    def register(self, role: Role, device: HardwareDevice) -> None:
        role = Role(role)
        iface = ROLE_INTERFACE[role]
        if not isinstance(device, iface):
            raise TypeError(f"{role.value}: {type(device).__name__} does not implement {iface.__name__}")
        if role in self._devices:
            raise RegistryError(f"role {role.value} already registered")
        self._devices[role] = device

    def get(self, role: Role) -> HardwareDevice | None:
        return self._devices.get(Role(role))

    def require(self, role: Role) -> HardwareDevice:
        dev = self._devices.get(Role(role))
        if dev is None:
            raise RegistryError(f"no device registered for role {Role(role).value}")
        return dev

    def roles(self) -> list[Role]:
        return list(self._devices)

    def items(self) -> Iterator[tuple[Role, HardwareDevice]]:
        return iter(list(self._devices.items()))

    def devices(self) -> list[HardwareDevice]:
        return list(self._devices.values())

    def __contains__(self, role: object) -> bool:
        try:
            return Role(role) in self._devices
        except ValueError:
            return False

    def __len__(self) -> int:
        return len(self._devices)
