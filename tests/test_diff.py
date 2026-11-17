import json

from openevos.inspector.analysis.diff import diff_scans
from openevos.inspector.app import build_models, main, run_scan
from openevos.inspector.reports.json_export import write_exports
from openevos.inspector.reports.markdown_export import render_diff_markdown
from openevos.inspector.schemas import DeviceInventory


def _write_scan(make_ctx, tmp_path, snap, roots=None, label=None):
    manifest, res, reg = run_scan(label or snap, make_ctx(snap, roots))
    out = tmp_path / (label or snap)
    write_exports(out, build_models(manifest, res, reg))
    return out


def test_diff_baseline_vs_microscope_with_recovered(make_ctx, tmp_path, fake_image):
    base = _write_scan(make_ctx, tmp_path, "baseline")
    scope = _write_scan(make_ctx, tmp_path, "microscope")
    rec = _write_scan(make_ctx, tmp_path, "baseline", [fake_image], label="recovered-image")

    d = diff_scans(base, scope, rec)
    added = {a.device.instance_id for a in d.added}
    assert added == {
        "USB\\VID_05E3&PID_0610\\5&1A2B3C4D&0&4",
        "USB\\VID_1234&PID_5678\\6&AAAA&0&1",
        "USB\\VID_1234&PID_5678&MI_00\\7&BBBB&0&0000",
        "USB\\VID_1234&PID_5678&MI_01\\7&BBBB&0&0001",
        "USB\\VID_ABCD&PID_0001\\SN0001TEST",
    }
    groups = {g.vid_pid: g for g in d.physical_groups}
    assert sorted(groups["1234:5678"].interface_ids) == [
        "USB\\VID_1234&PID_5678&MI_00\\7&BBBB&0&0000", "USB\\VID_1234&PID_5678&MI_01\\7&BBBB&0&0001"]
    mi00 = next(a for a in d.added if a.device.instance_id.endswith("0000") and "MI_00" in a.device.instance_id)
    assert mi00.driver.inf_name == "oem12.inf"
    assert [p.provider for p in mi00.recovered_inf_matches] == ["Life Technologies"]
    abcd = next(a for a in d.added if "ABCD" in a.device.instance_id)
    assert abcd.serial_ports == ["COM7"] and abcd.recovered_inf_matches == []
    assert [p.device for p in d.added_serial_ports] == ["COM7"]
    assert d.removed == [] and d.port_change_suspects == []

    md = render_diff_markdown(d)
    assert "Synthetic Test Camera" in md and "Unknown" in md


def test_port_change_detected(make_ctx, tmp_path):
    base = _write_scan(make_ctx, tmp_path, "microscope", label="m1")
    other = _write_scan(make_ctx, tmp_path, "microscope", label="m2")
    inv = DeviceInventory.model_validate_json((other / "device_inventory.json").read_text())
    for dev in inv.devices:  # simulate re-plugging the composite device into another port
        dev.instance_id = dev.instance_id.replace("6&AAAA&0&1", "6&CCCC&0&2")
        if dev.parent_instance_id:
            dev.parent_instance_id = dev.parent_instance_id.replace("6&AAAA&0&1", "6&CCCC&0&2")
    (other / "device_inventory.json").write_text(inv.model_dump_json())
    d = diff_scans(base, other)
    assert d.port_change_suspects == ["1234:5678"]


def test_cli_diff_writes_files(make_ctx, tmp_path, capsys):
    base = _write_scan(make_ctx, tmp_path, "baseline")
    scope = _write_scan(make_ctx, tmp_path, "microscope")
    assert main(["diff", str(base), str(scope)]) == 0
    data = json.loads((scope / "diff_baseline_vs_microscope.json").read_text())
    assert len(data["added"]) == 5


def test_candidate_roles_from_vendor_analysis(make_ctx, tmp_path):
    from openevos.inspector.analysis.rules import candidate_for, load_candidates

    cands = load_candidates()
    assert candidate_for("0DAD", "0058", cands)["role"] == "amc1000_controller"
    assert candidate_for("1409", "1234", cands)["role"] == "camera_color_cmos"  # vendor-wide entry
    assert candidate_for("1234", "5678", cands) is None
    assert all(c["label"] != "verified" for c in cands)  # vendor software is never proof about our unit
    base = _write_scan(make_ctx, tmp_path, "baseline")
    scope = _write_scan(make_ctx, tmp_path, "microscope")
    d = diff_scans(base, scope)
    assert all(a.candidate_role is None for a in d.added)  # synthetic fixtures use fake IDs
