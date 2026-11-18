import json
import sys

import pytest

from openevos.inspector.app import build_models, main, run_scan
from openevos.inspector.evidence import EvidenceLabel as L
from openevos.inspector.evidence import EvidenceRegister, EvidenceStage
from openevos.inspector.reports.json_export import write_exports
from openevos.inspector.reports.markdown_export import render_report
from openevos.inspector.reports.redaction import Redactor, collect_sensitive_values
from openevos.inspector.schemas import DeviceInventory


def _rules(reg, rule):
    return [f for f in reg.findings if f.rule_id == rule]


def test_baseline_scan_with_mocked_windows(make_ctx):
    ctx = make_ctx("baseline")
    manifest, res, reg = run_scan("baseline", ctx)

    status = {c.name: c.status for c in manifest.collectors}
    assert status["system"] == "ok" and status["windows_devices"] == "ok"
    assert status["driver_packages"] == "skipped"  # no Windows volume available in this test
    assert set(ctx.runner.calls) >= {"pnp_devices", "pnp_device_properties", "signed_drivers", "system_info", "pnp_events"}  # noqa: E501

    assert res.system.model == "OptiPlex 7090" and res.system.dotnet_framework_release == 528372
    by_id = {d.instance_id: d for d in res.devices}
    ftdi = by_id["USB\\VID_0403&PID_6001\\A12345BC"]
    assert ftdi.bus_reported_description == "FT232R USB UART"
    assert ftdi.parent_instance_id.startswith("USB\\ROOT_HUB30")
    assert ftdi.location_paths == ["PCIROOT(0)#PCI(1400)#USBROOT(0)#USB(2)"]
    assert ftdi.stage is EvidenceStage.DETECTED

    assert len(_rules(reg, "R-USB-PHANTOM")) == 1
    assert not _rules(reg, "R-PNP-PROBLEM")  # phantom's code 45 is not reported as a present-device problem
    assert [f.component for f in _rules(reg, "R-COM-USB")] == ["COM3"]
    assert {f.component for f in _rules(reg, "R-SW-KEYWORD")} == {"program:HKLM32:Synthetic EVOS Test Suite"}
    assert any("Life Technologies" in k.path for k in res.vendor_registry_keys)

    profile = _rules(reg, "R-PROFILE")
    assert len(profile) == 7 and all(f.label is L.NOT_TESTED for f in profile)
    # Inspector never exceeds IDENTIFIED; this scan never exceeds DETECTED
    assert all((f.stage or EvidenceStage.DETECTED) <= EvidenceStage.DETECTED for f in reg.findings)


def test_microscope_scan_flags_problem_and_unsigned(make_ctx):
    _m, _res, reg = run_scan("microscope", make_ctx("microscope"))
    probs = {f.component for f in _rules(reg, "R-PNP-PROBLEM")}
    assert probs == {"USB\\VID_1234&PID_5678&MI_01\\7&BBBB&0&0001", "USB\\VID_ABCD&PID_0001\\SN0001TEST"}
    assert {f.component for f in _rules(reg, "R-PNP-NODRIVER")} == probs
    assert len(_rules(reg, "R-DRV-UNSIGNED")) == 1
    assert {f.label for f in _rules(reg, "R-PROFILE")} == {L.UNKNOWN, L.NOT_TESTED}


def test_scan_with_recovered_image(make_ctx, fake_image):
    ctx = make_ctx("microscope", artifact_roots=[fake_image])
    _manifest, res, reg = run_scan("microscope", ctx)

    assert [p.source for p in res.packages] == ["recovered"]  # oem*.inf only; usb.inf ignored
    paths = {a.path for a in res.artifacts}
    assert any(p.endswith("EvosApp.exe") for p in paths) and any(p.endswith("calibration.xml") for p in paths)
    assert not any("evos_should_be_skipped" in p for p in paths)
    assert not any("unrelated.txt" in p for p in paths)
    exe = next(a for a in res.artifacts if a.path.endswith("EvosApp.exe"))
    assert exe.pe.is_managed and exe.pe.effective_bitness == "32" and exe.signature["status"] == "NotSigned"

    sdk = {m.signature_id: m for m in res.sdk_matches}
    assert set(sdk) == {"flir_flycapture2", "ftdi_d2xx"}
    assert sdk["flir_flycapture2"].label is L.PROBABLE  # filename only, no version resource
    assert res.micromanager[0].adapters == ["DemoCamera", "PointGrey"]

    xref = _rules(reg, "R-INF-XREF")
    assert len(xref) == 3 and all(f.label is L.STRONGLY_SUPPORTED for f in xref)  # composite + MI_00 + MI_01
    assert {f.component for f in _rules(reg, "R-PE-32BIT")} >= {exe.path}


def test_exports_roundtrip_and_report(make_ctx, fake_image, tmp_path):
    manifest, res, reg = run_scan("microscope", make_ctx("microscope", [fake_image]))
    out = tmp_path / "out"
    write_exports(out, build_models(manifest, res, reg))
    for name in ("system_inventory.json", "device_inventory.json", "driver_inventory.json",
                 "software_inventory.json", "evidence_register.json", "scan_manifest.json"):
        assert (out / name).exists()
    reg2 = EvidenceRegister.model_validate_json((out / "evidence_register.json").read_text())  # hashes re-checked
    assert len(reg2.evidence) == len(reg.evidence)
    for f in reg2.findings:
        assert all(reg2.has(e) for e in f.evidence_ids)

    md = render_report(manifest, res, reg)
    for heading in ("What Windows directly reported", "What drivers and files identify", "What is inferred",
                    "What remains unknown", "Next read-only steps", "Actions not performed"):
        assert heading in md
    assert "Synthetic Test Camera" in md


def test_redaction_removes_identifiers_and_stays_diffable(make_ctx, tmp_path):
    key = b"k" * 32
    outs = []
    for snap in ("baseline", "microscope"):
        manifest, res, reg = run_scan(snap, make_ctx(snap))
        red = Redactor(key, collect_sensitive_values(res))
        manifest.meta.redacted = True
        out = tmp_path / snap
        write_exports(out, build_models(manifest, res, reg), red)
        blob = "".join(p.read_text() for p in out.glob("*.json"))
        assert "LABPC-EVOS01" not in blob and "A12345BC" not in blob
        assert "0403" in blob  # VID/PID are not personal data and must survive
        EvidenceRegister.model_validate_json((out / "evidence_register.json").read_text())
        outs.append(DeviceInventory.model_validate_json((out / "device_inventory.json").read_text()))
    ftdi_ids = [next(d.instance_id for d in inv.devices if d.usb and d.usb.vid == "0403" and not d.usb.is_interface
                     and d.instance_id.startswith("USB")) for inv in outs]
    assert ftdi_ids[0] == ftdi_ids[1] and "<redacted:" in ftdi_ids[0]


def test_cli_refuses_output_inside_artifact_root(fake_image, capsys):
    rc = main(["scan", "--artifact-root", str(fake_image), "--out", str(fake_image / "scans")])
    assert rc == 2
    assert "refusing" in capsys.readouterr().err


@pytest.mark.skipif(sys.platform == "win32", reason="would run a real scan")
def test_cli_scan_on_non_windows_host(fake_image, tmp_path, capsys):
    rc = main(["scan", "--label", "recovered-image", "--artifact-root", str(fake_image),
               "--out", str(tmp_path / "scans"), "--no-default-roots"])
    assert rc == 0
    scan_dir = next((tmp_path / "scans").iterdir())
    manifest = json.loads((scan_dir / "scan_manifest.json").read_text())
    assert manifest["meta"]["label"] == "recovered-image"
    assert (scan_dir / "openevos_diagnostic_report.md").exists()


def test_candidate_role_rule_fires_for_known_ids(make_ctx):
    import copy

    import conftest

    class Runner(conftest.FakeRunner):
        def __call__(self, key, **kw):
            out = super().__call__(key, **kw)
            if key == "pnp_devices":
                out = copy.deepcopy(out) + [{
                    "InstanceId": "USB\\VID_0DAD&PID_0058\\5&ABC&0&2", "FriendlyName": "USB Input Device",
                    "Class": "HIDClass", "Status": "OK", "Present": True, "ConfigManagerErrorCode": 0,
                    "Service": "HidUsb", "HardwareID": ["USB\\VID_0DAD&PID_0058&REV_0100"]}]
            return out

    ctx = make_ctx("amc1000")
    ctx.runner = Runner("baseline")
    _m, _r, reg = run_scan("amc1000", ctx)
    f = [x for x in reg.findings if x.rule_id == "R-CANDIDATE-ROLE"]
    assert len(f) == 1 and f[0].label is L.STRONGLY_SUPPORTED and "amc1000_controller" in f[0].claim
    assert f[0].alternatives
