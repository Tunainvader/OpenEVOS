"""The MCP tools an assistant sees (ADR-0054, Phase 1: read-only).

Every tool is annotated read_only_hint=True and reaches the instrument only through
`InstrumentView`. Expected misses (no frame yet, empty slot) come back as readable tool errors
(`isError`); an unexpected crash returns a generic error and keeps its details in the app's log.
"""

from __future__ import annotations

from typing import Annotated, Any

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from openevos.mcp_link.view import (
    MAX_EVENTS,
    MAX_IMAGE_PX,
    MAX_PROBE_HZ,
    MAX_PROBE_S,
    InstrumentView,
)

SERVER_NAME = "openevos"
POLICY = (
    "Phase 1 is read-only. The AI actor may read and never command (plan §13.3, ADR-0003): "
    "no tool here moves an axis, changes light, exposure, objective or vessel, starts or stops "
    "an acquisition, or writes a file. Ask the operator to act in the OpenEVOS app."
)

INSTRUCTIONS = f"""OpenEVOS: an EVOS FL Auto microscope controller, observed through its running session.

Start with describe_api (once), then get_state. {POLICY}

Units: stage and focus positions in micrometres (um) in the frame each record names; times in
seconds. Values labelled synthetic/simulated come from the simulator, not the instrument: never
report them as measurements. stage_monitor reports the X/Y drift monitor (ADR-0040) when a real
stage is connected; a latched monitor means the stage refuses motion until the operator recovers it.
"""

GUIDE = {
    "policy": POLICY,
    "start": ["describe_api (once)", "get_state", "then the tool that answers the question"],
    "tools": {
        "describe_api": "this guide, the instrument profile and the operator command catalogue",
        "get_state": "one snapshot: positions, objective, light, vessel, every device's condition, "
                     "drift monitor summary, buffered image slots",
        "get_events": "command lifecycle, faults, acquisition status and device condition changes since "
                      "a sequence number; pass the returned last_seq next time",
        "probe": f"sample observed positions/activity over time (<= {MAX_PROBE_S:g} s, <= {MAX_PROBE_HZ:g} Hz)",
        "stage_monitor": "X/Y drift monitor state and recent samples (APOS vs CPOS, moving flags)",
        "list_images": "metadata of the images buffered in the capture panel",
        "get_image": f"a display-stretched PNG (<= {MAX_IMAGE_PX} px) of the live preview or a buffered slot",
    },
    "conventions": {
        "units": "um and s unless a field names another unit",
        "synthetic": "true means simulated data; say so whenever you report it",
        "errors": "returned as tool results with isError; read the message",
    },
}

RO = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True,
                     open_world_hint=False)
RO_LIVE = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=False,
                          open_world_hint=False)


def build_server(view: InstrumentView, *, version: str = "") -> MCPServer:
    mcp = MCPServer(name=SERVER_NAME, title="OpenEVOS microscope (read-only)", instructions=INSTRUCTIONS,
                    version=version)

    @mcp.tool(title="Describe the API", annotations=RO)
    def describe_api() -> dict[str, Any]:
        """Read once at the start: usage guide, read-only policy, instrument profile (cameras, light
        cubes, objectives) and the operator command catalogue, for reference (the AI may issue none)."""
        return {"guide": GUIDE, "profile": view.profile(), "operator_commands": view.command_catalog()}

    @mcp.tool(title="Get instrument state", annotations=RO_LIVE)
    def get_state() -> dict[str, Any]:
        """Current snapshot of the session: stage XY and focus Z (um), objective slot, light, vessel,
        every device's connection/health/activity/recovery, the drift monitor summary and buffered
        image slots."""
        return view.state()

    @mcp.tool(title="Get events", annotations=RO_LIVE)
    def get_events(
        since_seq: Annotated[int, Field(ge=0, description="return events after this sequence number")] = 0,
        limit: Annotated[int, Field(ge=1, le=MAX_EVENTS)] = 100,
        kinds: Annotated[list[str] | None, Field(
            description="filter: command, fault, storage_fault, acquisition, device")] = None,
    ) -> dict[str, Any]:
        """Events recorded since the MCP link started: command lifecycle, hardware and storage faults,
        acquisition status, device condition changes. Pass the returned last_seq as since_seq next time."""
        rows, last = view.events.since(since_seq, limit, set(kinds) if kinds else None)
        return {"events": [{"seq": e.seq, "kind": e.kind, "timestamp": e.timestamp, **e.data} for e in rows],
                "last_seq": last}

    @mcp.tool(title="Probe positions over time", annotations=RO_LIVE)
    def probe(
        device_ids: Annotated[list[str] | None, Field(description="devices to sample; default all")] = None,
        duration_s: Annotated[float, Field(ge=0, le=MAX_PROBE_S)] = 2.0,
        rate_hz: Annotated[float, Field(ge=0.5, le=MAX_PROBE_HZ)] = 10.0,
    ) -> dict[str, Any]:
        """Sample each device's observed position and activity from the runtime state store at a fixed
        rate. Reads cached state only: it adds no traffic to the drives."""
        return view.probe(device_ids, duration_s, rate_hz)

    @mcp.tool(title="Stage drift monitor", annotations=RO_LIVE)
    def stage_monitor(
        tail: Annotated[int, Field(ge=0, le=400, description="recent samples to include")] = 100,
    ) -> dict[str, Any]:
        """The X/Y drift monitor of each physical stage connection (ADR-0040): latched state, refusal
        reason, last poll, and recent samples (APOS, CPOS, moving flags). Empty in pure simulation."""
        return view.stage_monitor(tail)

    @mcp.tool(title="List buffered images", annotations=RO_LIVE)
    def list_images() -> dict[str, Any]:
        """Metadata of the images in the capture buffers (channel, camera, objective, pixel size, stage
        position, exposure, synthetic flag)."""
        return {"images": view.images()}

    @mcp.tool(title="Get image", annotations=RO_LIVE)
    def get_image(
        source: Annotated[str, Field(description='"live" for the latest preview frame, or a slot name '
                                                 'from list_images')] = "live",
        max_px: Annotated[int, Field(ge=64, le=MAX_IMAGE_PX)] = 512,
    ) -> list:
        """A downsampled, display-stretched PNG and its metadata. For looking at, not measuring: raw
        pixel data never leaves the app."""
        try:
            png, meta = view.image_png(source, max_px)
        except LookupError as exc:  # expected (no frame yet, empty slot): tell the assistant why
            raise ToolError(str(exc)) from exc
        return [Image(data=png, format="png"), meta]

    @mcp.resource("openevos://guide", title="OpenEVOS MCP guide", mime_type="application/json")
    def guide() -> dict[str, Any]:
        return GUIDE

    return mcp
