import json

import pytest
from pydantic import ValidationError

from openevos.ai.provider_base import NullProvider
from openevos.ai.schemas import AITriageResponse, validate_against_register
from openevos.inspector.app import build_models, main, run_scan
from openevos.inspector.reports.json_export import write_exports


@pytest.fixture
def scan(make_ctx):
    _m, res, reg = run_scan("microscope", make_ctx("microscope"))
    camera = next(d for d in res.devices if d.instance_id == "USB\\VID_1234&PID_5678\\6&AAAA&0&1")
    detected = next(f for f in reg.findings if f.rule_id == "R-USB-DETECTED" and f.component == camera.instance_id)
    return reg, camera, detected


def good(camera, detected):
    return {
        "summary": "A new composite USB device appeared with the microscope; its role is not established.",
        "findings": [{"component": camera.instance_id, "claim": "Device is present (see deterministic finding).",
                      "label": "strongly_supported", "confidence": 0.9, "evidence_ids": camera.evidence_ids,
                      "cites_findings": [detected.id]}],
        "hypotheses": [{"claim": "May be one of the camera interfaces.", "confidence": 0.45,
                        "alternatives": ["stage/turret controller", "USB hub function"],
                        "evidence_ids": camera.evidence_ids}],
        "unknowns": ["Camera vendor and model"],
        "recommended_checks": [
            {"action": "Compare the VID:PID with INF packages on the recovered disk image.", "risk": "read_only",
             "requires_operator": False},
            {"action": "Photograph the label on the rear controller board.", "risk": "operator_observation",
             "requires_operator": True}],
        "prohibited_inferences": ["Do not claim the camera is operational from enumeration alone."],
    }


def test_valid_response_accepted(scan):
    reg, camera, detected = scan
    resp = AITriageResponse.model_validate(good(camera, detected))
    assert validate_against_register(resp, reg) == []


@pytest.mark.parametrize("mutate,match", [
    (lambda r: r["findings"][0].update(label="verified", confidence=1.0), "may not label"),
    (lambda r: r["findings"][0].update(confidence=0.5), "outside band"),
    (lambda r: r["hypotheses"][0].update(alternatives=[]), "at least 1"),
    (lambda r: r["hypotheses"][0].update(confidence=1.0), "less than 1"),
    (lambda r: r["recommended_checks"][0].update(risk="actuation"), "risk"),
    (lambda r: r["recommended_checks"][1].update(requires_operator=False), "requires_operator"),
])
def test_schema_rejections(scan, mutate, match):
    _reg, camera, detected = scan
    r = good(camera, detected)
    mutate(r)
    with pytest.raises(ValidationError, match=match):
        AITriageResponse.model_validate(r)


def test_semantic_violations(scan):
    reg, camera, detected = scan
    r = good(camera, detected)
    r["hypotheses"][0]["evidence_ids"] = ["usb-deadbeef00"]
    r["findings"][0]["claim"] = "The camera is working."
    r["findings"][0]["cites_findings"] = ["R-NOPE-0000000000"]
    r["recommended_checks"] += [
        {"action": "Send command bytes 0xAA 0x55 to the endpoint to read the version.", "risk": "read_only", "requires_operator": False},  # noqa: E501
        {"action": "Move the stage to home position to test the controller.", "risk": "read_only", "requires_operator": False},  # noqa: E501
        {"action": "Uninstall the driver and reinstall from the vendor site.", "risk": "read_only", "requires_operator": False},  # noqa: E501
        {"action": "Run chkdsk on the old disk.", "risk": "read_only", "requires_operator": False},
    ]
    v = validate_against_register(AITriageResponse.model_validate(r), reg)
    msgs = " | ".join(x.message for x in v)
    assert "unknown evidence" in msgs and "unknown finding" in msgs and "working" in msgs
    assert sum("prohibited operation" in x.message for x in v) == 4


def test_null_provider_offline():
    out = NullProvider().analyze_inventory({})
    assert out.findings == [] and "not configured" in out.summary


def test_cli_validate_ai(make_ctx, tmp_path, scan, capsys):
    manifest, res, reg = run_scan("microscope", make_ctx("microscope"))
    write_exports(tmp_path / "s", build_models(manifest, res, reg))
    camera = next(d for d in res.devices if d.instance_id == "USB\\VID_1234&PID_5678\\6&AAAA&0&1")
    detected = next(f for f in reg.findings if f.rule_id == "R-USB-DETECTED" and f.component == camera.instance_id)
    p = tmp_path / "resp.json"
    p.write_text(json.dumps(good(camera, detected)))
    assert main(["validate-ai", str(p), "--scan", str(tmp_path / "s")]) == 0
    bad = good(camera, detected)
    bad["findings"][0]["label"] = "verified"
    bad["findings"][0]["confidence"] = 1.0
    p.write_text(json.dumps(bad))
    assert main(["validate-ai", str(p), "--scan", str(tmp_path / "s")]) == 1
    assert "REJECTED" in capsys.readouterr().out
