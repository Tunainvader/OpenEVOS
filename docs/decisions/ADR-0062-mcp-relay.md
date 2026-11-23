# ADR-0062: Relay for the read-only MCP link (reach from another PC)

- **Status:** Proposed (2026-11-24). Tested end to end on the simulator with the relay running under `wrangler dev`.
  **Not yet deployed to Cloudflare, and not yet used with the instrument.**
- **Asked for by:** the operator, 2026-11-24: "make the mcp connection for evos similar to craftimon webconnect, i
  want to connect from different desktop". Chosen option: a relay, like Craftimon.
- **Builds on:**
  - ADR-0054 (read-only MCP link; loopback, bearer token);
  - ADR-0003 (the AI actor reads and never commands).
- **Code:**
  - `relay/` (Cloudflare Worker: `src/index.ts`, `wrangler.jsonc`, `README.md`);
  - `src/openevos/mcp_link/relay_client.py`;
  - `mcp_link/cli.py` (`--relay`, `--relay-secret`, `--relay-revoke`);
  - `ui/app.py` (`--mcp-relay`, `_RelayLog`);
  - `tools/create_shortcuts.ps1` (`-Relay`);
  - `pyproject.toml` (`websockets` in the `mcp` extra).
- **Tests:**
  - `tests/test_mcp_relay.py`: a fake relay that speaks the same frames;
  - the end-to-end script `e2e_relay.py` (not in the test suite: it needs Node and wrangler), 31 checks, run
    2026-11-24.

## Context
`--mcp-allow <IP>` (fdce3ea) already lets a client on the same network reach the link over plain HTTP. That does not
cover the operator's case:
- **Claude connectors run in Anthropic's cloud.** Custom connectors in claude.ai and the Claude Desktop app call the
  MCP server from Anthropic's infrastructure, so the server must be reachable from the public internet over HTTPS. A
  lab PC's LAN address is not.
- **No inbound ports.** The microscope PC sits on an institute network that should not accept connections from
  outside, and should not have to.
- **Plain HTTP exposes the token.** A bearer token sent over plain HTTP can be read by anyone on the network path.

Craftimon solves the same problem with a hosted relay that the app connects out to.

## Decision
1. **A relay the operator owns.** One Cloudflare Worker per instrument, deployed from `relay/` into the operator's
   own account. It has three parts:
   - OAuth 2.1 authorization server: `@cloudflare/workers-oauth-provider`, pinned at 1.3.0, with dynamic client
     registration and PKCE. It is what a Claude custom connector expects.
   - Durable Object `Instrument`: holds the single WebSocket from the app.
   - `/mcp`: the protected resource. Scope `openevos:read`; POST only (stateless JSON, as the app's link).
2. **The app dials out.**
   - `openevos --mcp-relay <https URL>` starts the loopback link (ADR-0054) and a `RelayClient` thread. The client
     keeps a WebSocket to `<URL>/connect` open, authenticated with a 32-byte device secret.
   - The secret is created once in the OpenEVOS config folder (owner-only, like the MCP token) and printed by
     `openevos-mcp --relay-secret` for `wrangler secret put DEVICE_SECRET`.
   - Plain `ws://`/`http://` relay URLs are refused, except for localhost testing.
   - Reconnects with backoff (1 s to 60 s).
3. **The relay adds nothing.**
   - Each request is forwarded unchanged to `http://127.0.0.1:PORT/mcp` with the link's own bearer token. Only
     these headers pass: Content-Type, Accept, MCP-Protocol-Version, MCP-Session-Id.
   - So everything ADR-0054 guarantees still holds: the tool set, `readOnlyHint`, the AST test against
     state-changing calls, the Host checks.
   - `relay_client.py` imports nothing from OpenEVOS (tested). It cannot reach the session other than through the
     link.
4. **Sign-in that proves presence or ownership.** The relay's `/authorize` page:
   - names the client and the redirect host, and says what read-only access means;
   - accepts **either** an 8-digit one-time code, which the relay sends to the app at that moment and the app writes
     to its log (single use, 5 minutes, 5 tries, stored only as a hash), **or** the optional `OWNER_PASSPHRASE`
     Worker secret (10 tries per hour), for when nobody can see the microscope PC's screen.

   The app's log records each allowed sign-in, each connection and each remote request by method and tool name,
   never arguments. Connection events, but never codes, also go to the session fault log.
5. **Framing.** Bodies travel base64-encoded in JSON frames of at most 512 KiB raw. Cloudflare caps WebSocket
   messages at 1 MiB, and `get_image` can return more. Requests are limited to 1 MB; the relay waits up to 60 s for a
   reply and fails waiting requests at once when the app disconnects.
6. **Revocation.**
   - `openevos-mcp --relay-revoke <URL>` (authenticated with the device secret) revokes every grant.
   - Changing `DEVICE_SECRET` locks the app out until it is given the new one.
   - The library keeps one grant per client, so signing in again replaces the old grant.

## Consequences
- Claude on any PC (claude.ai, Desktop, Code, mobile) can observe the instrument while the app runs, with no firewall
  change at the lab.
- **Data leaves the lab.** State, events and downsampled images pass through Cloudflare and Anthropic when Claude
  reads them. The relay stores no bodies; the operator decides per session by starting with or without
  `--mcp-relay`.
- Cost: the free Cloudflare plan covers one instrument at these rates (KV for OAuth records, one SQLite-backed
  Durable Object). Confirm current limits when deploying.
- The relay is a second codebase (TypeScript) to keep current, mainly the pinned OAuth library.

## Verification (2026-11-24, simulator + `wrangler dev`)
`tests/test_mcp_relay.py`: 10 tests (URLs, secret file, end to end through a fake relay, frame splitting, sign-in
code and notices, wrong secret, reconnect, stop, no OpenEVOS imports, app log buffering). `e2e_relay.py`: 31/31,
covering:
- the OAuth challenge on `/mcp`, discovery documents and dynamic registration;
- the sign-in page; the code shown by the app; wrong code refused; right code redirects; PKCE token exchange;
- `initialize`, `tools/list` (7 read-only tools), `get_state` (23 ms round trip) and `probe` through the relay;
  notification 202; GET 405;
- refresh; passphrase sign-in; wrong passphrase refused; five wrong codes expire the code;
- revoke; the revoked token refused; a 503 while the app is stopped; reconnect after restart; wrong device secret
  refused.

Not verified:
- on Cloudflare itself (the 1 MiB message cap is handled by framing but was not exercised against the real limit);
- with the real claude.ai connector flow;
- on the instrument.

## Not decided here
- Write tools (proposals approved in the app) stay out of scope, as in ADR-0054. The relay would carry them
  unchanged, so a write phase needs its own ADR, including whether remote clients may propose at all.
- A shared, multi-instrument relay run by the project.
