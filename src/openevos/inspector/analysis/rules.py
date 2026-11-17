"""Deterministic rules over collected evidence -> labelled findings.

Every rule states which evidence it rests on. Rules never use a stage above DETECTED in v0.1:
"identified" requires a confirmed role mapping, which only an operator (with evidence) or a
future SDK enumeration can provide.
"""

from __future__ import annotations

import hashlib
import json
from importlib import resources

from openevos.inspector.collectors import ScanResults
from openevos.inspector.evidence import (
    EvidenceLabel,
    EvidenceRegister,
    EvidenceStage,
    Finding,
    Origin,
)

L = EvidenceLabel


def _fid(rule: str, component: str) -> str:
    return f"{rule}-{hashlib.sha256(component.upper().encode()).hexdigest()[:10]}"


def load_candidates() -> list[dict]:
    text = resources.files("openevos.inspector.data").joinpath("candidate_ids.json").read_text("utf-8")
    return json.loads(text)["candidates"]


def candidate_for(vid: str | None, pid: str | None, candidates: list[dict] | None = None) -> dict | None:
    """Best candidate role for a VID:PID (exact PID beats a vendor-wide '*' entry)."""
    if not vid:
        return None
    cands = candidates if candidates is not None else load_candidates()
    exact = [c for c in cands if c["vid"] == vid and c["pid"] == pid]
    wild = [c for c in cands if c["vid"] == vid and c["pid"] == "*"]
    return (exact or wild or [None])[0]


def load_profile() -> dict:
    text = resources.files("openevos.inspector.data").joinpath("evos_fl_auto_profile.json").read_text("utf-8")
    return json.loads(text)


class _Emitter:
    def __init__(self, register: EvidenceRegister):
        self.reg = register
        self.ids: set[str] = {f.id for f in register.findings}

    def emit(self, rule: str, component: str, claim: str, label: EvidenceLabel, evidence_ids: list[str],
             confidence: float | None = None, stage: EvidenceStage | None = None,
             alternatives: list[str] | None = None, note: str = "") -> None:
        fid = _fid(rule, component)
        if fid in self.ids:
            return
        if label is L.VERIFIED and confidence is None:
            confidence = 1.0
        self.reg.add_finding(
            Finding(
                id=fid, component=component, claim=claim, label=label, confidence=confidence,
                evidence_ids=evidence_ids, origin=Origin.RULE, rule_id=rule, stage=stage,
                alternatives=alternatives or [], note=note,
            )
        )
        self.ids.add(fid)


def run_rules(results: ScanResults, register: EvidenceRegister, snapshot_label: str) -> None:
    e = _Emitter(register)

    # --- PnP / USB ---------------------------------------------------------------------------
    for d in results.devices:
        if d.usb is None:
            continue
        vp = d.usb.vid_pid or "?"
        kind = f"interface MI_{d.usb.mi}" if d.usb.is_interface else "device"
        if d.present is False:
            e.emit("R-USB-PHANTOM", d.instance_id,
                   f"Windows retains a record of USB {kind} {vp} that is not currently attached.",
                   L.VERIFIED, d.evidence_ids,
                   note="Useful history: it was attached to this PC at some point.")
            continue
        e.emit("R-USB-DETECTED", d.instance_id, f"Windows enumerates USB {kind} {vp}.",
               L.VERIFIED, d.evidence_ids, stage=EvidenceStage.DETECTED)
        if d.problem_code not in (None, 0):
            e.emit("R-PNP-PROBLEM", d.instance_id,
                   f"Windows reports problem code {d.problem_code} ({d.problem or 'unnamed'}) for USB {kind} {vp}.",
                   L.VERIFIED, d.evidence_ids,
                   note="A problem code describes the driver/PnP state; it is not evidence of a hardware fault.")
        if not d.service and (d.status or "").upper() != "OK":
            e.emit("R-PNP-NODRIVER", d.instance_id,
                   f"No function driver is bound to USB {kind} {vp} (status: {d.status or 'unknown'}).",
                   L.VERIFIED, d.evidence_ids)

    for drv in results.drivers:
        if drv.is_signed is False:
            e.emit("R-DRV-UNSIGNED", drv.device_id,
                   f"Driver {drv.inf_name or '?'} ({drv.provider or 'unknown provider'}) is reported unsigned.",
                   L.VERIFIED, drv.evidence_ids)

    for p in results.serial_ports:
        if p.vid and p.pid:
            e.emit("R-COM-USB", p.device,
                   f"{p.device} is provided by USB device {p.vid}:{p.pid} ({p.description or 'no description'}).",
                   L.VERIFIED, p.evidence_ids,
                   note="Port listed from metadata only; it was not opened.")

    # --- Software / files --------------------------------------------------------------------
    for prog in results.programs:
        if prog.keyword_hits:
            e.emit("R-SW-KEYWORD", f"program:{prog.registry_view}:{prog.name}",
                   f"Installed program '{prog.name}' may relate to the EVOS installation "
                   f"(matches: {', '.join(prog.keyword_hits)}).",
                   L.PROBABLE, prog.evidence_ids, confidence=0.5,
                   alternatives=["unrelated product from the same vendor", "keyword coincidence"])

    for art in [*results.artifacts]:
        if art.pe and art.pe.effective_bitness == "32":
            e.emit("R-PE-32BIT", art.path, f"PE header shows a 32-bit binary ({art.pe.machine}, {art.pe.pe_format}).",
                   L.VERIFIED, art.evidence_ids,
                   note="A 32-bit SDK cannot load into a 64-bit Python process; plan for a 32-bit helper process.")
    for m in results.sdk_matches:
        conf = 0.8 if m.label is L.STRONGLY_SUPPORTED else 0.55
        e.emit("R-SDK-MATCH", m.path, f"File appears to belong to {m.vendor} {m.sdk} ({'; '.join(m.basis)}).",
               m.label, m.evidence_ids, confidence=conf,
               alternatives=["same-named file from unrelated software", "SDK installed for other hardware"],
               note="Presence on disk does not show that the EVOS uses this SDK.")
        if m.pe and m.pe.effective_bitness == "32":
            e.emit("R-PE-32BIT", m.path, f"PE header shows a 32-bit binary ({m.pe.machine}, {m.pe.pe_format}).",
                   L.VERIFIED, m.evidence_ids)

    for pkg in results.packages:
        if pkg.keyword_hits:
            e.emit("R-INF-KEYWORD", pkg.inf_path,
                   f"Third-party driver package {pkg.inf_path} (provider: {pkg.provider or '?'}) may belong to the "
                   f"EVOS installation (matches: {', '.join(pkg.keyword_hits)}).",
                   L.PROBABLE, pkg.evidence_ids, confidence=0.6,
                   alternatives=["driver for other equipment from the same vendor", "keyword coincidence"])

    # Cross-reference: live USB device VID:PID listed in a third-party INF (live or recovered)
    for d in results.devices:
        if d.usb is None or not d.usb.vid_pid:
            continue
        for pkg in results.packages:
            if d.usb.vid_pid in pkg.usb_ids:
                strong = bool(pkg.keyword_hits)
                e.emit("R-INF-XREF", f"{d.instance_id}|{pkg.inf_path}",
                       f"USB {d.usb.vid_pid} ({d.instance_id}) is listed in driver package {pkg.inf_path} "
                       f"(provider: {pkg.provider or '?'}).",
                       L.STRONGLY_SUPPORTED if strong else L.PROBABLE, [*d.evidence_ids, *pkg.evidence_ids],
                       confidence=0.8 if strong else 0.5,
                       alternatives=["INF lists many IDs; this one may be unrelated", "shared bridge-chip VID/PID"])

    # Candidate role mapping from static vendor-software analysis (never 'verified')
    candidates = load_candidates()
    for d in results.devices:
        if d.usb is None or d.present is False or d.usb.is_interface:
            continue
        c = candidate_for(d.usb.vid, d.usb.pid, candidates)
        if c is None:
            continue
        e.emit("R-CANDIDATE-ROLE", d.instance_id,
               f"USB {d.usb.vid_pid} matches the vendor-software candidate for role '{c['role']}': {c['device']}.",
               EvidenceLabel(c["label"]), d.evidence_ids, confidence=c["confidence"],
               alternatives=c["alternatives"],
               note="Source: static analysis of vendor installers (docs/evidence/vendor_software_analysis.md). "
                    "Expected by the software; not yet confirmed on this unit.")

    for mm in results.micromanager:
        e.emit("R-MM-PRESENT", mm.path, f"Micro-Manager directory found with {len(mm.adapters)} device adapter DLL(s).",
               L.VERIFIED, mm.evidence_ids)

    # --- Expected-profile comparison ---------------------------------------------------------
    profile = load_profile()
    for comp in profile["components"]:
        role = comp["role"]
        if snapshot_label != comp["snapshot"]:
            label, claim = L.NOT_TESTED, (
                f"{comp['description']}: not tested in this snapshot (needs a '{comp['snapshot']}' snapshot and diff)."
            )
        else:
            label, claim = L.UNKNOWN, (
                f"{comp['description']}: no device has been mapped to this role yet. Use `diff` against the "
                "baseline to list candidates; mapping requires further evidence."
            )
        e.emit("R-PROFILE", f"role:{role}", claim, label, [])
