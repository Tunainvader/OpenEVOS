"""COM-port *listing* via pyserial metadata. Ports are never opened, read or written."""

from __future__ import annotations

from openevos.inspector.collectors import Collector, CollectorContext, CollectorSkipped, ScanResults
from openevos.inspector.schemas import SerialPort


def _hex4(v) -> str | None:
    return f"{int(v):04X}" if isinstance(v, int) else None


class SerialPortsCollector(Collector):
    name = "serial_ports"
    description = "Serial port metadata (pyserial list_ports; no port is opened)"

    def collect(self, ctx: CollectorContext, results: ScanResults) -> str | None:
        lister = ctx.serial_lister
        if lister is None:
            try:
                from serial.tools import list_ports
            except ImportError as exc:
                raise CollectorSkipped("pyserial not installed") from exc
            lister = list_ports.comports
        ctx.op("serial-port-enumeration (metadata only)")
        for p in lister():
            data = {
                k: getattr(p, k, None)
                for k in ("device", "name", "description", "hwid", "vid", "pid",
                          "serial_number", "location", "manufacturer", "product", "interface")
            }
            eid = ctx.add_evidence(self, "com", str(data["device"]), "pyserial:list_ports", data)
            results.serial_ports.append(
                SerialPort(
                    device=str(data["device"]),
                    description=data["description"],
                    hwid=data["hwid"],
                    vid=_hex4(data["vid"]),
                    pid=_hex4(data["pid"]),
                    serial_number=data["serial_number"],
                    manufacturer=data["manufacturer"],
                    product=data["product"],
                    location=data["location"],
                    evidence_ids=[eid],
                )
            )
        return None
