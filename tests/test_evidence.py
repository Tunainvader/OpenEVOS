import json

import pytest
from pydantic import ValidationError

from openevos.core.models import Activity, BackendKind
from openevos.hal.status_model import RuntimeState, activity_is_admissible, state_is_admissible
from openevos.inspector.evidence import (
    Evidence,
    EvidenceRegister,
    EvidenceStage,
    Finding,
    Origin,
    RegisterError,
    check_confidence,
    label_for_confidence,
    make_evidence_id,
)
from openevos.inspector.evidence import (
    EvidenceLabel as L,
)


def ev(key="k", data=None):
    return Evidence.create(prefix="usb", natural_key=key, collector="t", collector_version="0",
                           source="test", data=data or {"a": 1})


def test_six_labels_and_five_stages():
    assert len(L) == 6
    assert [s.name for s in EvidenceStage] == [
        "DETECTED", "IDENTIFIED", "CONNECTED", "RESPONSIVE", "FUNCTIONALLY_VALIDATED"]
    assert EvidenceStage.DETECTED < EvidenceStage.FUNCTIONALLY_VALIDATED


@pytest.mark.parametrize("label,conf,ok", [
    (L.VERIFIED, 1.0, True), (L.VERIFIED, 0.99, False), (L.VERIFIED, None, False),
    (L.STRONGLY_SUPPORTED, 0.75, True), (L.STRONGLY_SUPPORTED, 1.0, False),
    (L.PROBABLE, 0.40, True), (L.PROBABLE, 0.75, False), (L.PROBABLE, 0.2, False),
    (L.UNKNOWN, None, True), (L.UNKNOWN, 0.5, False), (L.NOT_TESTED, None, True),
])
def test_confidence_bands(label, conf, ok):
    if ok:
        check_confidence(label, conf)
    else:
        with pytest.raises(ValueError):
            check_confidence(label, conf)


def test_label_for_confidence_never_verified():
    assert label_for_confidence(1.0) is L.STRONGLY_SUPPORTED
    assert label_for_confidence(0.5) is L.PROBABLE
    assert label_for_confidence(0.1) is L.UNKNOWN


def test_stable_ids_case_insensitive():
    assert make_evidence_id("usb", "USB\\VID_1") == make_evidence_id("usb", "usb\\vid_1")


def test_evidence_tamper_detected_on_reload():
    e = ev()
    blob = json.loads(e.model_dump_json())
    blob["data"]["a"] = 2
    with pytest.raises(ValidationError, match="sha256"):
        Evidence.model_validate(blob)


def test_assertive_finding_needs_evidence():
    with pytest.raises(ValidationError):
        Finding(id="f", component="c", claim="something", label=L.PROBABLE, confidence=0.5, origin=Origin.RULE)
    Finding(id="f", component="c", claim="nothing known", label=L.UNKNOWN, origin=Origin.RULE)


def test_ai_cannot_mint_verified():
    with pytest.raises(ValidationError, match="AI-origin"):
        Finding(id="f", component="c", claim="present", label=L.VERIFIED, confidence=1.0,
                evidence_ids=["usb-0000000000"], origin=Origin.AI)


def test_ai_probable_needs_alternatives():
    with pytest.raises(ValidationError, match="alternative"):
        Finding(id="f", component="c", claim="maybe a camera", label=L.PROBABLE, confidence=0.5,
                evidence_ids=["x-00000000"], origin=Origin.AI)


def test_stage_requires_verified():
    with pytest.raises(ValidationError):
        Finding(id="f", component="c", claim="detected", label=L.PROBABLE, confidence=0.5,
                evidence_ids=["x-00000000"], origin=Origin.RULE, stage=EvidenceStage.DETECTED)


def test_register_rejects_unknown_evidence_and_stage_cap():
    reg = EvidenceRegister(scan_id="s")
    e = ev()
    reg.add_evidence(e)
    with pytest.raises(RegisterError, match="unknown evidence"):
        reg.add_finding(Finding(id="a", component="c", claim="x present", label=L.VERIFIED, confidence=1.0,
                                evidence_ids=["usb-ffffffffff"], origin=Origin.RULE))
    with pytest.raises(RegisterError, match="capped"):
        reg.add_finding(Finding(id="b", component="c", claim="responds", label=L.VERIFIED, confidence=1.0,
                                evidence_ids=[e.id], origin=Origin.RULE, stage=EvidenceStage.RESPONSIVE))
    reg.add_finding(Finding(id="c", component="c", claim="enumerated", label=L.VERIFIED, confidence=1.0,
                            evidence_ids=[e.id], origin=Origin.RULE, stage=EvidenceStage.DETECTED))
    assert reg.component_stage("c") is EvidenceStage.DETECTED


def test_register_id_collision():
    reg = EvidenceRegister(scan_id="s")
    reg.add_evidence(ev("k", {"a": 1}))
    reg.add_evidence(ev("k", {"a": 1}))  # identical: idempotent
    with pytest.raises(RegisterError, match="collision"):
        reg.add_evidence(ev("k", {"a": 2}))


def test_runtime_state_requires_stage():
    assert not state_is_admissible(RuntimeState.READY, EvidenceStage.DETECTED)
    assert state_is_admissible(RuntimeState.READY, EvidenceStage.RESPONSIVE)
    assert state_is_admissible(RuntimeState.UNKNOWN, None)
    # ADR-0007 three-axis model
    assert not activity_is_admissible(Activity.IDLE, EvidenceStage.DETECTED)
    assert activity_is_admissible(Activity.IDLE, EvidenceStage.RESPONSIVE)
    assert activity_is_admissible(Activity.UNKNOWN, None)
    assert not activity_is_admissible(Activity.MOVING, None)
    assert activity_is_admissible(Activity.MOVING, None, BackendKind.SIMULATED)
