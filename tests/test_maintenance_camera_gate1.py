import json

import numpy as np
import pytest

from openevos.maintenance import camera_gate1 as g

MONO = g.ROLES["mono"]


class FakePnp:
    def __init__(self, devices):
        self.devices = devices

    def present(self, vid, pid):
        return [d for d in self.devices if (d.vid, d.pid) == (vid, pid)]


def dev(role=MONO, code=0, n=1):
    return [g.PnpDevice(f"USB\\VID_{role.vid:04X}&PID_{role.pid:04X}\\X{i}", role.vid, role.pid, code)
            for i in range(n)]


class FakeCamera:
    def __init__(self, mean=40, shape=(964, 1296), fail=None, sdk=True, count=1):
        self.calls, self.mean, self.shape, self.fail, self.sdk, self.count = [], mean, shape, fail, sdk, count

    def sdk_available(self):
        return self.sdk

    def count_cameras(self):
        return self.count

    def _do(self, name, value=None):
        self.calls.append(name)
        if self.fail == name:
            raise OSError("boom")
        return value

    def connect(self):
        self._do("connect")

    def identity(self):
        return self._do("identity", {"model": "Chameleon CMLN-13S2M", "serial": "123"})

    def modes(self):
        return self._do("modes", {"format7": [[1296, 964, "MONO16"]]})

    def grab_one(self, timeout_s):
        px = np.full(self.shape, self.mean, dtype=np.uint16)
        return self._do("grab_one", (px, 12))

    def close(self):
        self._do("close")


def test_phrases():
    assert MONO.phrase == "READ CAMERA MONO 1E10:2005 WITH LED OFF"
    assert g.ROLES["colour"].phrase == "READ CAMERA COLOUR 1409:1465 WITH LED OFF"


def test_backend_has_no_setters():
    names = {n for n in dir(g.CameraGateBackend) if not n.startswith("_")}
    assert names == {"sdk_available", "count_cameras", "connect", "identity", "modes", "grab_one", "close"}


@pytest.mark.parametrize("devices,why", [([], "found 0"), (dev(n=2), "found 2"), (dev(code=28), "problem code 28")])
def test_pnp_preconditions_refuse(devices, why):
    cam = FakeCamera()
    r = g.run_gate(MONO, FakePnp(devices), cam, send=True, confirm=MONO.phrase)
    assert r.outcome == "refused" and why in r.message and cam.calls == [] and not r.opened


def test_sdk_missing_and_count_refuse():
    for cam in (FakeCamera(sdk=False), FakeCamera(count=0)):
        r = g.run_gate(MONO, FakePnp(dev()), cam, send=True, confirm=MONO.phrase)
        assert r.outcome == "refused" and cam.calls == []


def test_dry_run_by_default_opens_nothing():
    cam = FakeCamera()
    r = g.run_gate(MONO, FakePnp(dev()), cam)
    assert r.outcome == "dry_run" and cam.calls == []


def test_wrong_phrase_refuses():
    cam = FakeCamera()
    r = g.run_gate(MONO, FakePnp(dev()), cam, send=True, confirm="READ CAMERA MONO")
    assert r.outcome == "refused" and cam.calls == []


def test_complete_dark_frame_passes(tmp_path):
    cam = FakeCamera()
    r = g.run_gate(MONO, FakePnp(dev()), cam, send=True, confirm=MONO.phrase, evidence_dir=tmp_path)
    assert r.passed and r.outcome == "complete"
    assert cam.calls == ["connect", "identity", "modes", "grab_one", "close"]
    assert r.frame_stats["mean"] == 40 and r.frame_stats["saturated"] == 0
    assert (tmp_path / r.frame_file).exists()
    json.loads(r.to_json())


def test_bright_frame_is_not_a_pass():
    r = g.run_gate(MONO, FakePnp(dev()), FakeCamera(mean=4095), send=True, confirm=MONO.phrase)
    assert r.outcome == "complete" and not r.passed and "not dark" in r.message


def test_wrong_shape_stops():
    r = g.run_gate(MONO, FakePnp(dev()), FakeCamera(shape=(480, 640)), send=True, confirm=MONO.phrase)
    assert r.outcome == "stopped" and not r.passed


@pytest.mark.parametrize("step", ["connect", "identity", "modes", "grab_one"])
def test_failure_stops_once_and_still_closes(step):
    cam = FakeCamera(fail=step)
    r = g.run_gate(MONO, FakePnp(dev()), cam, send=True, confirm=MONO.phrase)
    assert r.outcome == "stopped" and step in r.message
    assert cam.calls.count(step) == 1 and cam.calls[-1] == "close"


def test_placeholder_backend_refuses_on_hardware():
    r = g.run_gate(MONO, FakePnp(dev()), g.SdkNotIntegrated(MONO), send=True, confirm=MONO.phrase)
    assert r.outcome == "refused" and "FlyCapture2_C_v140.dll" in r.message


def test_main_dry_run_writes_evidence(tmp_path, capsys):
    rc = g.main(["--role", "mono", "--evidence-dir", str(tmp_path)], pnp=FakePnp(dev()), backend=FakeCamera())
    assert rc == 0 and "[dry_run]" in capsys.readouterr().out
    assert len(list(tmp_path.glob("camera-gate1-mono-*-dry_run.json"))) == 1
