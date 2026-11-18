"""Start the LED board application at app launch, as the vendor software does every session (ADR-0055, amends
ADR-0012; operator decision 2026-11-18 after "every time I power-cycle or restart the app I find the light
disconnected").

The board always powers up in its bootloader (0DAD:0057). This runs the two reviewed steps unchanged:

1. Gate 1 (`led_bootloader.query_version`): one version query `0x0A`; it must return a valid reply that evidences an
   application image;
2. StartApp (`led_startapp.start_app`): one `0x08` report, then enumeration only until the application (0DAD:0051 or
   0052) appears, at most 30 s.

Every precondition of those tools still applies: exactly one bootloader interface, no application interface present,
VID/PID and 65/65-byte reports confirmed, a fresh Gate 1 record. Each step writes its JSON evidence record. Nothing is
retried; nothing is ever sent to the application from here; no firmware, EEPROM or calibration write exists in either
step. If the application is already running, or no board is present, nothing is sent at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from openevos.maintenance import led_bootloader as g1
from openevos.maintenance import led_startapp as sa

#: on the instrument, StartApp brought the application up in about 1 s (2026-11-17, twice)
APPLICATION_PIDS = sa.PID_APPLICATION


@dataclass(frozen=True)
class AutostartResult:
    outcome: str  # "already_running" | "absent" | "started" | "refused" | "failed"
    message: str
    evidence: tuple[str, ...] = ()

    @property
    def light_available(self) -> bool:
        return self.outcome in ("already_running", "started")


def autostart(backend=None, *, evidence_dir: Path) -> AutostartResult:
    backend = backend or g1.WindowsHid()
    if any(backend.find(g1.VID, pid) for pid in APPLICATION_PIDS):
        return AutostartResult("already_running", "LED board application already running; nothing sent")
    if not backend.find(g1.VID, g1.PID_BOOTLOADER):
        return AutostartResult("absent", "no LED board on USB (instrument off or unplugged?); nothing sent")
    ev: list[str] = []
    r1 = g1.query_version(backend, send=True, confirm=g1.CONFIRM_PHRASE)
    if r1.sent or r1.outcome != "refused":
        ev.append(g1.write_evidence(r1, evidence_dir).name)
    if r1.outcome != "reply" or not (r1.decoded and r1.decoded.application_image_evidenced):
        return AutostartResult("refused" if not r1.sent else "failed", f"LED board version query: {r1.message}",
                               tuple(ev))
    # the checklist phrase is supplied by this launch path: the operator chose vendor-like start-up (ADR-0055)
    r2 = sa.start_app(backend, evidence_dir=evidence_dir, send=True, confirm=sa.CONFIRM_PHRASE,
                      checklist=sa.CHECKLIST_PHRASE)
    if r2.sent:
        ev.append(sa.write_evidence(r2, evidence_dir).name)
    if r2.outcome == "app_appeared":
        return AutostartResult("started", f"LED board application started after {r2.app_appeared_after_s:.1f} s",
                               tuple(ev))
    return AutostartResult("refused" if not r2.sent else "failed", f"LED board start: {r2.message}", tuple(ev))


def main(argv: list[str] | None = None) -> int:
    """`python -m openevos.maintenance.led_autostart`: the same start-up step as the app's launch, on its own."""
    import argparse
    import sys

    p = argparse.ArgumentParser(prog="python -m openevos.maintenance.led_autostart", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--evidence-dir", type=Path, default=Path.cwd() / "openevos-scans")
    a = p.parse_args(argv)
    if sys.platform != "win32":
        print("This tool needs Windows (HID via SetupAPI).", file=sys.stderr)
        return 2
    r = autostart(evidence_dir=a.evidence_dir)
    print(f"[{r.outcome}] {r.message}")
    for e in r.evidence:
        print(f"evidence: {a.evidence_dir / e}")
    return 0 if r.light_available else 1


if __name__ == "__main__":
    raise SystemExit(main())
