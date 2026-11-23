# OpenEVOS relay

Lets Claude on **any computer** (claude.ai, the Claude Desktop app, Claude Code) reach the read-only MCP link of the
OpenEVOS app running on the microscope PC. Decision record: [ADR-0062](../docs/decisions/ADR-0062-mcp-relay.md).

```
Claude (any PC) --HTTPS + sign-in--> relay (your Cloudflare Worker) <--WebSocket, outbound-- OpenEVOS (microscope PC)
```

- **No open port on the microscope PC.** OpenEVOS connects out to the relay, like a browser does.
- **Same tools, same limits.** The relay passes each MCP request to the app's own loopback link
  (`127.0.0.1:8765/mcp`). The app's tools are read only (ADR-0054): no tool can move, light, capture or change
  anything.
- **Two credentials.**
  - The app proves itself to the relay with a *device secret*.
  - Claude signs in with OAuth. The sign-in page asks for a one-time code that OpenEVOS shows in its log at that
    moment, or for an owner passphrase (useful when nobody can see the microscope PC's screen).
- **You own it.** The relay runs in your Cloudflare account. It keeps no requests, replies or images: they pass
  through memory only.

## Deploy (once, about 15 minutes)

You need a free Cloudflare account and Node.js 20 or newer on any computer.

```sh
cd relay
npm install
npx wrangler login                          # opens the browser
npx wrangler kv namespace create OAUTH_KV   # copy the printed id into wrangler.jsonc (REPLACE_WITH_KV_NAMESPACE_ID)
```

On the **microscope PC**, print the device secret (it is created once and kept, owner-only, in the OpenEVOS config
folder):

```sh
openevos-mcp --relay-secret
```

Back in `relay/`:

```sh
npx wrangler secret put DEVICE_SECRET       # paste the secret from the microscope PC
npx wrangler secret put OWNER_PASSPHRASE    # optional: a long passphrase for signing in without the code
npx wrangler deploy                         # prints https://openevos-relay.<your-subdomain>.workers.dev
```

Open that URL in a browser: it says whether the microscope is connected.

## Connect

**Microscope PC.** Start OpenEVOS with the relay:

```sh
openevos --mcp-relay https://openevos-relay.<your-subdomain>.workers.dev   # plus your usual options
```

Or recreate the desktop shortcuts with it:

```powershell
powershell -ExecutionPolicy Bypass -File tools\create_shortcuts.ps1 -Relay https://openevos-relay.<your-subdomain>.workers.dev
```

The log shows `Relay connected: Claude can reach this read-only link at https://…/mcp`.

**Other PC, claude.ai or the Claude Desktop app.**
1. Customize › Connectors › Add › Custom › Web.
2. Name it `OpenEVOS` and give the URL `https://openevos-relay.<your-subdomain>.workers.dev/mcp`.
3. Connect. A sign-in page opens.
4. Enter the 8-digit code that OpenEVOS has just written to its log (`Relay sign-in code: 1234 5678`), or the owner
   passphrase, and click Allow.

**Other PC, Claude Code.**

```sh
claude mcp add --transport http openevos https://openevos-relay.<your-subdomain>.workers.dev/mcp
```

Then run `/mcp` in Claude Code to sign in (same page, same code).

Sign-ins last 30 days of use (access tokens refresh every hour).

## Remove access

- Every sign-in at once: `openevos-mcp --relay-revoke https://openevos-relay.<your-subdomain>.workers.dev`.
- Disconnect the microscope: start OpenEVOS without `--mcp-relay`. The relay then answers
  `503 OpenEVOS is not connected`.
- Replace the device secret:
  1. Delete `mcp_relay_secret` in the OpenEVOS config folder.
  2. Run `openevos-mcp --relay-secret` and `npx wrangler secret put DEVICE_SECRET` again.
  3. Restart OpenEVOS.

## Develop and test locally

```sh
printf 'DEVICE_SECRET=%s\nOWNER_PASSPHRASE=test passphrase\n' "$(python -c 'print("d"*43)')" > .dev.vars
npx wrangler dev --port 8787
openevos-mcp --http --port 0 --relay http://127.0.0.1:8787   # simulator; put the same secret in mcp_relay_secret
```

Plain `http://` relay URLs are accepted only for `localhost`/`127.0.0.1`. `npm run check` type-checks the Worker.

## Limits

- **One instrument per relay.** A second OpenEVOS connecting with the same secret replaces the first.
- **Images leave the lab.** An image Claude reads passes through Cloudflare and Anthropic. Leave the relay off for
  confidential samples.
- **Requests and timeouts.** Requests up to 1 MB; replies are split into 512 KiB frames, because Cloudflare caps
  WebSocket messages at 1 MiB. A request waits up to 60 s.
- **Sign-in code spam.** Anyone who knows the relay URL can open the sign-in page and make OpenEVOS log a new code.
  This can't grant access without the code, but each new code replaces the last one. If it becomes a nuisance,
  rename the Worker.
