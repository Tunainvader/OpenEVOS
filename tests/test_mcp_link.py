"""MCP link, Phase 1 (ADR-0054): read-only tools over a simulated session, and the local HTTP guard.

Every value here comes from the simulator (synthetic); nothing describes the real instrument.
"""

from __future__ import annotations

import ast
import json
import urllib.error
import urllib.request
from pathlib import Path

import anyio
import pytest

pytest.importorskip("mcp")

from mcp import Client

from openevos.acquisition.session import build_sim_session
from openevos.core.clock import ThreadClock
from openevos.core.event_bus import CommandLifecycleEvent
from openevos.mcp_link.server import build_server
from openevos.mcp_link.view import MAX_PROBE_S, InstrumentView

PKG = Path(__file__).resolve().parents[1] / "src" / "openevos" / "mcp_link"
TOKEN = "t" * 43

# Calls that change instrument, session or file state. The modules the tools can reach must make none.
FORBIDDEN = {
    "execute", "stop_all", "submit", "run", "acquire", "capture", "capture_all", "capture_all_channels",
    "start_live", "stop_live", "change_objective", "select_vessel", "save_settings", "apply_camera",
    "put", "shutdown", "note", "publish", "recover", "clear_latch", "home", "write_text", "write_bytes",
    "unlink", "rename", "mkdir", "capture_focus_nominal", "snap_to_last",
}
FORBIDDEN_PREFIXES = ("set_", "move", "jog", "write", "stop", "start", "enable", "disable", "reset")


def _calls(path: Path) -> list[tuple[int, str]]:
    out = []
    for node in ast.walk(ast.parse(path.read_text("utf-8"))):
        if isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else None
            if name:
                out.append((node.lineno, name))
    return out


@pytest.mark.parametrize("module", ["view.py", "server.py"])
def test_tool_reachable_code_makes_no_state_changing_call(module):
    bad = [(ln, n) for ln, n in _calls(PKG / module)
           if n in FORBIDDEN or n.startswith(FORBIDDEN_PREFIXES)]
    assert not bad, f"{module} must stay read-only (ADR-0003/0054): {bad}"


def test_tool_reachable_code_never_imports_the_command_gateway():
    for module in ("view.py", "server.py"):
        src = (PKG / module).read_text("utf-8")
        assert "core_gateway" not in src and "CoreGateway" not in src, module


@pytest.fixture
def sim():
    s = build_sim_session(ThreadClock(), fast=True, fps=60)
    view = InstrumentView(s)
    yield s, view
    view.close()
    s.shutdown()


def _call(server, name, args=None):
    async def go():
        async with Client(server) as c:
            return await c.call_tool(name, args or {})
    return anyio.run(go)


def _json(result) -> dict:
    if result.structured_content is not None:
        sc = result.structured_content
        return sc.get("result", sc) if isinstance(sc, dict) and set(sc) == {"result"} else sc
    return json.loads(result.content[0].text)


def test_every_tool_is_annotated_read_only(sim):
    _, view = sim

    async def go():
        async with Client(build_server(view)) as c:
            return (await c.list_tools()).tools
    tools = anyio.run(go)
    names = {t.name for t in tools}
    assert names == {"describe_api", "get_state", "get_events", "probe", "stage_monitor", "list_images",
                     "get_image"}
    for t in tools:
        assert t.annotations and t.annotations.read_only_hint is True, t.name
        assert t.annotations.destructive_hint is False, t.name


def test_describe_api_marks_every_operator_command_ai_forbidden(sim):
    _, view = sim
    d = _json(_call(build_server(view), "describe_api"))
    assert "read-only" in d["guide"]["policy"]
    cmds = d["operator_commands"]
    assert any(c["name"] == "xy.move_absolute" for c in cmds)
    assert all(c["ai_may_issue"] is False for c in cmds)
    assert {o["slot"] for o in d["profile"]["objectives"]}


def test_get_state_reports_simulated_session(sim):
    s, view = sim
    st = _json(_call(build_server(view), "get_state"))
    assert st["simulated_optics"] is True and st["physical_stage"] is False
    assert st["stage_device"] == s.profile.stage_device
    ids = {d["device_id"] for d in st["devices"]}
    assert s.profile.stage_device in ids and s.profile.focus_device in ids
    assert all(d["backend"] == "simulated" for d in st["devices"])
    assert set(st["simulated_devices"]) == ids  # every device simulated here
    assert st["stage_monitor"] == []  # no physical stage, no drift monitor


def test_probe_is_bounded_and_samples_positions(sim):
    s, view = sim
    r = view.probe([s.profile.stage_device, "nope"], duration_s=0.2, rate_hz=20)
    rows = r["series"][s.profile.stage_device]
    assert len(rows) == 5 and rows[0][2] is not None
    assert r["unknown_devices"] == ["nope"]
    assert r["axes"][s.profile.stage_device]["unit"] == "um"


def test_probe_clamps_without_sleeping_long(sim):
    s, _ = sim
    slept = []
    v = InstrumentView(s, sleep=slept.append)
    try:
        r = v.probe([s.profile.stage_device], duration_s=1e6, rate_hz=1e6)
    finally:
        v.close()
    assert r["duration_s"] == MAX_PROBE_S and r["rate_hz"] == 50.0
    assert len(r["series"][s.profile.stage_device]) == int(MAX_PROBE_S * 50) + 1


def test_tools_publish_no_command(sim):
    s, view = sim
    seen = []
    sub = s.core.bus.subscribe(CommandLifecycleEvent, seen.append)
    try:
        server = build_server(view)
        for name, args in [("describe_api", {}), ("get_state", {}), ("get_events", {}),
                           ("probe", {"duration_s": 0.1}), ("stage_monitor", {}), ("list_images", {}),
                           ("get_image", {})]:
            _call(server, name, args)
    finally:
        sub.unsubscribe()
    assert seen == []


def test_events_and_images_follow_operator_actions(sim):
    s, view = sim
    server = build_server(view)
    # no image yet: a readable tool error, not a protocol failure
    r = _call(server, "get_image", {"source": "cube1"})
    assert r.is_error and "no buffered image" in r.content[0].text

    s.capture.capture("trans")  # the operator's Capture button (simulated camera and light)

    ev = _json(_call(server, "get_events", {"kinds": ["command"]}))
    assert ev["events"] and ev["last_seq"] >= ev["events"][-1]["seq"]
    assert {e["kind"] for e in ev["events"]} == {"command"}
    again = _json(_call(server, "get_events", {"since_seq": ev["last_seq"]}))
    assert again["events"] == []

    imgs = _json(_call(server, "list_images"))["images"]
    assert [i["slot"] for i in imgs] == ["trans"] and imgs[0]["synthetic"] is True
    assert imgs[0]["pixel_size_calibrated"] is False  # simulator: nominal pixel size only

    r = _call(server, "get_image", {"source": "trans", "max_px": 128})
    assert not r.is_error
    kinds = [c.type for c in r.content]
    assert "image" in kinds
    png = next(c for c in r.content if c.type == "image")
    assert png.mime_type == "image/png"
    meta = json.loads(next(c for c in r.content if c.type == "text").text)
    assert meta["synthetic"] is True and meta["display"]["downsample"] >= 1


# ---------------------------------------------------------------------- local HTTP
@pytest.fixture
def http_link(sim):
    from openevos.mcp_link.local_http import LocalMcpService

    _, view = sim
    svc = LocalMcpService(build_server(view), port=0, token=TOKEN).start()
    yield svc
    svc.stop()


def _post(url, headers):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json", "Accept": "application/json, text/event-stream", **headers})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code


def test_http_refuses_non_loopback_bind(sim):
    from openevos.mcp_link.local_http import LocalMcpService

    with pytest.raises(ValueError):
        LocalMcpService(build_server(sim[1]), port=0, token=TOKEN, host="0.0.0.0")


def test_http_requires_token(http_link):
    assert _post(http_link.url, {}) == 401
    assert _post(http_link.url, {"Authorization": "Bearer wrong"}) == 401


def test_http_rejects_browser_origin_and_foreign_host(http_link):
    auth = {"Authorization": f"Bearer {TOKEN}"}
    assert _post(http_link.url, {**auth, "Origin": "https://evil.example"}) == 403
    assert _post(http_link.url, {**auth, "Host": f"evil.example:{http_link.port}"}) == 421


def test_http_client_with_token_reads_state(http_link):
    import httpx2
    from mcp.client.streamable_http import streamable_http_client

    async def go():
        headers = {"Authorization": f"Bearer {TOKEN}"}
        async with httpx2.AsyncClient(headers=headers) as http, \
                Client(streamable_http_client(http_link.url, http_client=http)) as c:
            return await c.call_tool("get_state", {})
    st = _json(anyio.run(go))
    assert st["simulated_optics"] is True


def test_token_file_is_owner_only(tmp_path):
    import os
    import stat

    from openevos.mcp_link.local_http import load_or_create_token

    p = tmp_path / "cfg" / "mcp_token"
    t1 = load_or_create_token(p)
    assert len(t1) >= 32 and load_or_create_token(p) == t1
    if os.name == "posix":
        assert stat.S_IMODE(p.stat().st_mode) == 0o600
