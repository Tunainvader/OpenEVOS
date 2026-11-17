# ADR-0054: Read-only MCP link for AI assistants (Phase 1)

- **Status:** Accepted (2026-11-17). Implemented and tested on the simulator. **Not yet used with the instrument.**
- **Asked for by:** the operator, 2026-11-17: "build mcp connection to the app similar to craftimon".
- **Builds on:**
  - ADR-0003 (read-only AI evidence boundary; plan §13.3: the AI actor may read and never command).
  - ADR-0008/0010 (command core; the UI runs on it).
  - ADR-0040 (X/Y drift monitor).
- **Code:** `src/openevos/mcp_link/` (`view.py`, `server.py`, `local_http.py`, `cli.py`); `ui/app.py` (`--mcp`);
  `pyproject.toml` (`mcp` extra, `openevos-mcp` script).
- **Tests:** `tests/test_mcp_link.py`.

## Context
An assistant such as Claude Code or Claude Desktop can help most when it can see what the instrument is doing:
positions, device health, faults, the drift monitor and the image on screen. The Model Context Protocol (MCP) is
the standard way to give it that view. Craftimon Studio's MCP server is the model for the shape of the tools: a
`describe_api` guide read once, a state snapshot, sampling over time (`probe`), and small images.

Two constraints decide the design:
1. ADR-0003: the AI may read and may never command. `CoreGateway.execute` already rejects `Actor.AI`.
2. One owner of the hardware. A second process opening COM3 or the cameras would race the app.

## Decision
1. **In the app, not beside it.** `openevos --mcp [PORT]` starts the link inside the running app, on the same
   session. There is still one owner of the hardware, and the assistant sees exactly what the operator sees.
   `openevos-mcp` is a separate, **simulator-only** server for trying the tools and for CI. It never opens
   hardware.
2. **Read-only by construction.**
   - The tools reach the session only through `mcp_link.view.InstrumentView`. It reads:
     - the runtime state store;
     - the controller's read methods;
     - the command catalogue (as reference);
     - event-bus subscriptions;
     - the drift monitor's state;
     - the capture buffers and the latest preview frame.
   - `tests/test_mcp_link.py` scans the AST of `view.py` and `server.py`. It fails on any state-changing call
     (`execute`, `stop_all`, `capture`, `start_live`, `set_*`, `move*`, `write*`, `put`, `shutdown`, ...) and on
     any import of the command gateway.
   - The test also runs every tool and asserts that no command lifecycle event appears.
   - Every tool carries `readOnlyHint: true`, `destructiveHint: false`.
3. **Tools (Phase 1).**

   | Tool | Returns |
   |---|---|
   | `describe_api` | guide, policy, instrument profile, operator command catalogue (each `ai_may_issue: false`) |
   | `get_state` | stage XY / focus Z (µm), objective, light, vessel, every device's snapshot, drift-monitor summary, buffered slots |
   | `get_events` | command lifecycle, hardware/storage faults, acquisition status, device *condition* changes, by sequence number |
   | `probe` | observed position and activity sampled from the state store, ≤ 10 s, ≤ 50 Hz (no drive traffic) |
   | `stage_monitor` | ADR-0040 monitor state and recent APOS/CPOS samples |
   | `list_images` | metadata of buffered captures |
   | `get_image` | PNG ≤ 1024 px, linear 0.1–99.9 % stretch, plus metadata. For looking at, not measuring; raw data stays in the app |

   - Values from the simulator keep their `synthetic: true` flag. The server instructions tell the assistant
     never to report them as measurements.
   - Expected misses (no preview frame, empty slot) return readable tool errors. Crash details stay in the app's log.
4. **Transport and access.**
   - Streamable HTTP, stateless, JSON responses, at `http://127.0.0.1:PORT/mcp`. Any other bind address is
     refused in code.
   - Every request needs `Authorization: Bearer <token>`, compared in constant time; otherwise 401. The token is
     created once in the user config directory (`platformdirs`), owner-only (0600).
   - The SDK's DNS-rebinding protection is on:
     - Host must be `127.0.0.1:PORT` or `localhost:PORT`, else 421;
     - **any** `Origin` header is refused with 403, so no web page can reach the link.
   - stdio is used only by the simulator-only CLI.
   - Connect Claude Code with:
     `claude mcp add --transport http openevos http://127.0.0.1:8765/mcp --header "Authorization: Bearer <token>"`.
     The app prints this line on start.

## Not decided here (later phases, each needs its own ADR)
- **Simulator writes and hardware writes.** These would change ADR-0003. The direction considered:
  - the assistant may only *propose* a typed command;
  - the app shows it in an approval queue;
  - the operator approves it in the app (never in the chat);
  - the core then executes it as `Actor.OPERATOR`, with the audit log naming the proposal.

  Firmware, EEPROM and drive-parameter writes stay out of reach in every phase.
- **Claude Desktop.** Desktop runs local servers over stdio. A small stdio→loopback bridge, packaged as a `.mcpb`
  bundle, would connect it to the running app without a second hardware owner.
- **Live push.** Events are read by polling `get_events`; resource subscriptions can come later.

## Consequences
- The assistant can diagnose from live state (for example the Y-drift work: `stage_monitor` samples, faults,
  recovery flags) without the operator copying logs.
- Nothing the assistant does can move, light, expose or write anything. A later change that wants to will fail
  the guard test until an ADR changes this one.
- New dependency, optional: `mcp>=2.3,<3` (the `mcp` extra; brings `uvicorn` and `starlette`).
