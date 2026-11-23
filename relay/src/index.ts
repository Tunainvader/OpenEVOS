/**
 * OpenEVOS relay (ADR-0062): lets Claude reach the read-only MCP link of an OpenEVOS app on another PC.
 *
 *   Claude (claude.ai, Claude Desktop, Claude Code)  --HTTPS + OAuth-->  this Worker  <--WebSocket--  OpenEVOS app
 *
 * The app dials out (no open port on the lab PC) and authenticates with DEVICE_SECRET. Claude signs in with OAuth;
 * the sign-in page asks for a one-time code that the OpenEVOS app shows at that moment, or for OWNER_PASSPHRASE.
 * Each MCP request is passed unchanged to the app, which answers it from its loopback link: the relay adds no
 * tools, keeps no request or reply, and cannot do more than the link itself (read only, ADR-0054).
 */
import { DurableObject } from "cloudflare:workers";
import OAuthProvider, { AuthorizationError, CimdFetchError } from "@cloudflare/workers-oauth-provider";
import type { ConsentDescription, OAuthHelpers } from "@cloudflare/workers-oauth-provider";

export interface Env {
  OAUTH_KV: KVNamespace;
  INSTRUMENT: DurableObjectNamespace<Instrument>;
  DEVICE_SECRET: string; // the app's credential (wrangler secret put DEVICE_SECRET)
  OWNER_PASSPHRASE?: string; // optional: sign in when nobody can read the app's screen
  OAUTH_PROVIDER: OAuthHelpers;
}

const SCOPE = "openevos:read";
const OWNER = "owner";
const CHUNK = 512 * 1024; // raw bytes per WebSocket frame (Cloudflare caps a message at 1 MiB)
const MAX_BODY = 1024 * 1024; // largest MCP request accepted from Claude
const REQUEST_TIMEOUT_MS = 60_000;
const CODE_TTL_MS = 5 * 60_000;
const CODE_ATTEMPTS = 5;
const PASSPHRASE_ATTEMPTS_PER_HOUR = 10;
const FORWARD_HEADERS = ["content-type", "accept", "mcp-protocol-version", "mcp-session-id", "last-event-id"];
const RETURN_HEADERS = ["content-type", "mcp-session-id"];

// ------------------------------------------------------------------------------------------------ helpers
const enc = new TextEncoder();
const dec = new TextDecoder();
const escape = (v: string) => v.replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);

async function sha256(s: string): Promise<ArrayBuffer> {
  return crypto.subtle.digest("SHA-256", enc.encode(s));
}

/** Constant-time comparison of two strings (hash both so the lengths match). */
async function sameSecret(a: string, b: string): Promise<boolean> {
  if (!a || !b) return false;
  const [x, y] = await Promise.all([sha256(a), sha256(b)]);
  return crypto.subtle.timingSafeEqual(x, y);
}

function b64(bytes: Uint8Array): string {
  let s = "";
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(s);
}

function unb64(s: string): Uint8Array {
  const bin = atob(s);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

function concat(parts: Uint8Array[]): Uint8Array {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let o = 0;
  for (const p of parts) {
    out.set(p, o);
    o += p.length;
  }
  return out;
}

function chunks(bytes: Uint8Array): string[] {
  const out: string[] = [];
  for (let i = 0; i < bytes.length || i === 0; i += CHUNK) out.push(b64(bytes.subarray(i, i + CHUNK)));
  return out;
}

function text(status: number, body: string, headers: Record<string, string> = {}): Response {
  return new Response(body, { status, headers: { "content-type": "text/plain; charset=utf-8", ...headers } });
}

function html(body: string, headers?: Headers, status = 200): Response {
  const h = headers ?? new Headers();
  h.set("content-type", "text/html; charset=utf-8");
  h.set("x-frame-options", "DENY");
  h.set("content-security-policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'");
  h.set("referrer-policy", "no-referrer");
  return new Response(body, { status, headers: h });
}

function instrument(env: Env): DurableObjectStub<Instrument> {
  return env.INSTRUMENT.get(env.INSTRUMENT.idFromName("default"));
}

// ------------------------------------------------------------------------------------------------ Durable Object
type Pending = { parts: Uint8Array[]; resolve: (r: Forwarded) => void; timer: ReturnType<typeof setTimeout>; meta?: { status: number; headers: Record<string, string> } };
export type Forwarded = { status: number; headers: Record<string, string>; body: string };
type CodeState = { hash: string; expires: number; attempts: number };

/** Holds the one WebSocket from the OpenEVOS app and passes MCP requests over it. */
export class Instrument extends DurableObject<Env> {
  private pending = new Map<string, Pending>();
  private incoming = new Map<string, Uint8Array[]>();

  private app(): WebSocket | undefined {
    return this.ctx.getWebSockets("app")[0];
  }

  async fetch(request: Request): Promise<Response> {
    // only the Worker calls this, after checking DEVICE_SECRET
    if (request.headers.get("upgrade")?.toLowerCase() !== "websocket") return text(426, "WebSocket upgrade required");
    for (const old of this.ctx.getWebSockets("app")) old.close(4000, "replaced by a newer OpenEVOS connection");
    const [client, server] = Object.values(new WebSocketPair());
    this.ctx.acceptWebSocket(server, ["app"]);
    this.ctx.setWebSocketAutoResponse(new WebSocketRequestResponsePair('{"t":"ping"}', '{"t":"pong"}'));
    server.serializeAttachment({ since: new Date().toISOString(), hello: null });
    return new Response(null, { status: 101, webSocket: client });
  }

  async webSocketMessage(ws: WebSocket, message: string | ArrayBuffer): Promise<void> {
    let m: Record<string, unknown>;
    try {
      m = JSON.parse(typeof message === "string" ? message : dec.decode(message));
    } catch {
      return;
    }
    if (m.t === "hello") {
      const att = ws.deserializeAttachment() ?? {};
      ws.serializeAttachment({ ...att, hello: { version: String(m.version ?? ""), link: String(m.link ?? "") } });
      return;
    }
    if (m.t !== "res" || typeof m.id !== "string") return;
    const p = this.pending.get(m.id);
    if (!p) return;
    if (m.seq === 0) p.meta = { status: Number(m.status ?? 502), headers: (m.headers as Record<string, string>) ?? {} };
    p.parts.push(unb64(String(m.data ?? "")));
    if (m.last) {
      clearTimeout(p.timer);
      this.pending.delete(m.id);
      const meta = p.meta ?? { status: 502, headers: {} };
      p.resolve({ status: meta.status, headers: meta.headers, body: dec.decode(concat(p.parts)) });
    }
  }

  async webSocketClose(ws: WebSocket, code: number): Promise<void> {
    // an app that went away fails what was waiting on it at once, not after the timeout
    for (const [id, p] of this.pending) {
      clearTimeout(p.timer);
      p.resolve({ status: 503, headers: { "content-type": "text/plain" }, body: "OpenEVOS disconnected from the relay" });
      this.pending.delete(id);
    }
    try {
      ws.close(code === 1005 ? 1000 : code, "closed");
    } catch {
      /* already closed */
    }
  }

  /** Pass one MCP request to the app and wait for its reply. */
  async forward(body: string, headers: Record<string, string>): Promise<Forwarded> {
    const ws = this.app();
    if (!ws) return { status: 503, headers: { "content-type": "text/plain" }, body: "OpenEVOS is not connected to the relay. Start it with --mcp-relay on the microscope PC." };
    const id = crypto.randomUUID();
    const result = new Promise<Forwarded>((resolve) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        resolve({ status: 504, headers: { "content-type": "text/plain" }, body: "OpenEVOS did not answer in time" });
      }, REQUEST_TIMEOUT_MS);
      this.pending.set(id, { parts: [], resolve, timer });
    });
    const parts = chunks(enc.encode(body));
    parts.forEach((data, seq) => {
      ws.send(JSON.stringify({ t: "req", id, seq, last: seq === parts.length - 1, data, ...(seq === 0 ? { headers } : {}) }));
    });
    return result;
  }

  /** Make a new one-time sign-in code and show it in the app. */
  async newCode(client: string): Promise<{ connected: boolean }> {
    const ws = this.app();
    if (!ws) return { connected: false };
    const digits = crypto.getRandomValues(new Uint32Array(2));
    const code = `${String(digits[0] % 10000).padStart(4, "0")}${String(digits[1] % 10000).padStart(4, "0")}`;
    const state: CodeState = { hash: b64(new Uint8Array(await sha256(code))), expires: Date.now() + CODE_TTL_MS, attempts: 0 };
    await this.ctx.storage.put("code", state);
    ws.send(JSON.stringify({ t: "pair_code", code, expires_s: CODE_TTL_MS / 1000, client: client.slice(0, 80) }));
    return { connected: true };
  }

  /** Check a typed code: single use, five tries, five minutes. */
  async checkCode(typed: string): Promise<"ok" | "wrong" | "expired"> {
    const state = await this.ctx.storage.get<CodeState>("code");
    if (!state || Date.now() > state.expires || state.attempts >= CODE_ATTEMPTS) {
      await this.ctx.storage.delete("code");
      return "expired";
    }
    const digits = typed.replace(/\D/g, "");
    const ok = await sameSecret(b64(new Uint8Array(await sha256(digits))), state.hash);
    if (ok) {
      await this.ctx.storage.delete("code");
      return "ok";
    }
    state.attempts += 1;
    await this.ctx.storage.put("code", state);
    return state.attempts >= CODE_ATTEMPTS ? "expired" : "wrong";
  }

  /** Rate limit for passphrase sign-in. */
  async passphraseAttempt(): Promise<boolean> {
    const hour = Math.floor(Date.now() / 3_600_000);
    const key = `pp:${hour}`;
    const n = ((await this.ctx.storage.get<number>(key)) ?? 0) + 1;
    await this.ctx.storage.put(key, n);
    await this.ctx.storage.delete(`pp:${hour - 1}`);
    return n <= PASSPHRASE_ATTEMPTS_PER_HOUR;
  }

  /** Tell the app who just connected (shown in its log). */
  async notify(event: string, detail: string): Promise<void> {
    this.app()?.send(JSON.stringify({ t: "notice", event, detail: detail.slice(0, 200) }));
  }

  async status(): Promise<{ connected: boolean; since?: string; version?: string }> {
    const ws = this.app();
    if (!ws) return { connected: false };
    const att = ws.deserializeAttachment() ?? {};
    return { connected: true, since: att.since, version: att.hello?.version };
  }
}

// ------------------------------------------------------------------------------------------------ MCP endpoint
const mcpApi = {
  async fetch(request: Request, env: Env, ctx: ExecutionContext & { auth?: { scope?: string[]; clientId?: string } }): Promise<Response> {
    if (request.method !== "POST") {
      // stateless JSON responses (as the app's link): no server-sent event stream to open or close
      return text(405, "POST only", { allow: "POST" });
    }
    if (!(ctx.auth?.scope ?? []).includes(SCOPE)) return text(403, "missing scope " + SCOPE);
    const raw = new Uint8Array(await request.arrayBuffer());
    if (raw.length > MAX_BODY) return text(413, "request too large");
    const headers: Record<string, string> = {};
    for (const h of FORWARD_HEADERS) {
      const v = request.headers.get(h);
      if (v) headers[h] = v;
    }
    const r = await instrument(env).forward(dec.decode(raw), headers);
    const out = new Headers({ "cache-control": "no-store" });
    for (const h of RETURN_HEADERS) if (r.headers[h]) out.set(h, r.headers[h]);
    return new Response(r.status === 202 ? null : r.body, { status: r.status, headers: out });
  },
};

// ------------------------------------------------------------------------------------------------ sign-in page
function page(title: string, inner: string): string {
  return `<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>${escape(title)}</title>
<style>
body{font:16px/1.5 system-ui,sans-serif;max-width:34rem;margin:3rem auto;padding:0 1rem;color:#1d1d1f}
h1{font-size:1.4rem}.box{border:1px solid #d2d2d7;border-radius:12px;padding:1rem 1.25rem;margin:1rem 0}
input{font:1.4rem ui-monospace,monospace;letter-spacing:.2em;padding:.4rem .6rem;width:100%;box-sizing:border-box}
button{font:inherit;padding:.5rem 1.1rem;border-radius:8px;border:1px solid #888;background:#fff;cursor:pointer}
button.primary{background:#0a66c2;border-color:#0a66c2;color:#fff}.err{color:#b00020}.muted{color:#6e6e73;font-size:.9rem}
</style><body>${inner}</body></html>`;
}

function consentPage(d: ConsentDescription, handle: string, env: Env, connected: boolean, error = ""): string {
  const name = escape(d.clientName || "An app");
  const origin = d.clientDomain ? `Published by <strong>${escape(d.clientDomain)}</strong>.` : "This app registered itself; its name is not verified.";
  const how = connected
    ? "Enter the 8-digit code that OpenEVOS shows now (in its log on the microscope PC)."
    : "OpenEVOS is not connected to the relay, so it cannot show a code.";
  const pass = env.OWNER_PASSPHRASE ? (connected ? " Or enter the owner passphrase." : " Enter the owner passphrase instead.") : "";
  return page(`Connect ${d.clientName || "an app"} to OpenEVOS`, `
<h1>Allow ${name} to read your OpenEVOS microscope?</h1>
<p>${origin} Access will be sent to <strong>${escape(d.redirectHost)}</strong>.</p>
${d.redirectIsLoopback ? "<p><strong>This sends access to an app on your computer.</strong> Continue only if you just started connecting from it.</p>" : ""}
<div class="box"><strong>Read only.</strong> It can see positions, device health, faults, the drift monitor and images.
It cannot move, light, capture or change anything. Images it reads pass through this relay and the app that asked.</div>
<form method="post">
<input type="hidden" name="handle" value="${escape(handle)}">
<p>${how}${pass}</p>
${error ? `<p class="err">${escape(error)}</p>` : ""}
<p><input name="secret" autocomplete="one-time-code" inputmode="text" autofocus ${connected || env.OWNER_PASSPHRASE ? "" : "disabled"}></p>
<p><button class="primary" name="decision" value="approve">Allow</button> <button name="decision" value="deny">Deny</button></p>
</form>
<p class="muted">Remove access later with <code>openevos-mcp --relay-revoke</code>, or by changing DEVICE_SECRET.</p>`);
}

async function authorize(request: Request, env: Env): Promise<Response> {
  const oauth = env.OAUTH_PROVIDER;
  const stub = instrument(env);
  try {
    if (request.method === "GET") {
      const req = await oauth.parseAuthRequest(request);
      const details = await oauth.describeConsent(req);
      const consent = await oauth.beginConsent(req);
      const { connected } = await stub.newCode(details.clientName || "an app");
      return html(consentPage(details, consent.handle, env, connected), consent.headers);
    }
    if (request.method !== "POST") return text(405, "method not allowed");
    const form = await request.formData();
    const handle = String(form.get("handle") ?? "");
    if (form.get("decision") !== "approve") {
      const denied = await oauth.denyConsent(request, handle);
      return new Response(null, { status: 302, headers: denied.headers });
    }
    const secret = String(form.get("secret") ?? "").trim();
    let via = "";
    if (/^[\d\s-]{8,11}$/.test(secret)) {
      const r = await stub.checkCode(secret);
      if (r === "ok") via = "code";
      else return retry(request, env, handle, r === "expired" ? "That code has expired or had too many tries. Start connecting again from Claude to get a new one." : "Wrong code. Check the OpenEVOS log and try again.");
    } else if (env.OWNER_PASSPHRASE && secret) {
      if (!(await stub.passphraseAttempt())) return retry(request, env, handle, "Too many passphrase attempts. Try again in an hour.");
      if (await sameSecret(secret, env.OWNER_PASSPHRASE)) via = "passphrase";
      else return retry(request, env, handle, "Wrong passphrase.");
    } else {
      return retry(request, env, handle, "Enter the code from OpenEVOS.");
    }
    const approved = await oauth.approveConsent(request, handle, { scope: [SCOPE] });
    const client = await oauth.lookupClient(approved.request.clientId);
    const { redirectTo } = await oauth.completeAuthorization({
      request: approved.request,
      userId: OWNER,
      metadata: { client: client?.clientName ?? approved.request.clientId, via, at: new Date().toISOString() },
      scope: [SCOPE],
      props: { userId: OWNER, via },
    });
    await stub.notify("connected", `${client?.clientName ?? "An app"} was allowed to read OpenEVOS (by ${via})`);
    approved.headers.set("Location", redirectTo);
    return new Response(null, { status: 302, headers: approved.headers });
  } catch (error) {
    if (error instanceof AuthorizationError && error.redirectTo) return Response.redirect(error.redirectTo, 302);
    if (error instanceof AuthorizationError || error instanceof CimdFetchError) {
      const message = error instanceof AuthorizationError ? error.description : "This app could not be verified.";
      return html(page("Cannot connect", `<h1>Cannot connect</h1><p>${escape(message ?? "Start again from Claude.")}</p>`), undefined, 400);
    }
    throw error;
  }
}

/** Show the page again after a wrong entry. The consent handle is still unused, so the same form works. */
async function retry(request: Request, env: Env, handle: string, error: string): Promise<Response> {
  const { connected } = await instrument(env).status();
  // describeConsent needs the request: it is kept with the handle, so render a minimal page instead of re-reading it
  return html(page("Connect to OpenEVOS", `
<h1>Connect to OpenEVOS</h1><p class="err">${escape(error)}</p>
<form method="post"><input type="hidden" name="handle" value="${escape(handle)}">
<p>${connected ? "Enter the 8-digit code that OpenEVOS shows." : "OpenEVOS is not connected."}${env.OWNER_PASSPHRASE ? " Or enter the owner passphrase." : ""}</p>
<p><input name="secret" autocomplete="one-time-code" autofocus></p>
<p><button class="primary" name="decision" value="approve">Allow</button> <button name="decision" value="deny">Deny</button></p></form>`), undefined, 400);
}

// ------------------------------------------------------------------------------------------------ other routes
const site = {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === "/authorize") return authorize(request, env);
    if (url.pathname === "/connect") {
      // the OpenEVOS app's WebSocket
      const got = (request.headers.get("authorization") ?? "").replace(/^Bearer\s+/i, "");
      if (!env.DEVICE_SECRET || env.DEVICE_SECRET.length < 32) return text(500, "DEVICE_SECRET is not set (wrangler secret put DEVICE_SECRET)");
      if (!(await sameSecret(got, env.DEVICE_SECRET))) return text(401, "wrong device secret");
      return instrument(env).fetch(request);
    }
    if (url.pathname === "/revoke" && request.method === "POST") {
      // the app (holding DEVICE_SECRET) removes every sign-in at once
      const got = (request.headers.get("authorization") ?? "").replace(/^Bearer\s+/i, "");
      if (!(await sameSecret(got, env.DEVICE_SECRET))) return text(401, "wrong device secret");
      let n = 0;
      let cursor: string | undefined;
      do {
        const page = await env.OAUTH_PROVIDER.listUserGrants(OWNER, { cursor });
        for (const g of page.items) {
          await env.OAUTH_PROVIDER.revokeGrant(g.id, OWNER);
          n++;
        }
        cursor = page.cursor;
      } while (cursor);
      return Response.json({ revoked: n });
    }
    if (url.pathname === "/" && request.method === "GET") {
      const s = await instrument(env).status();
      return html(page("OpenEVOS relay", `<h1>OpenEVOS relay</h1>
<p>Microscope: <strong>${s.connected ? "connected" : "not connected"}</strong>.</p>
<p>Add <code>${escape(url.origin)}/mcp</code> as a custom connector in Claude.</p>`));
    }
    return text(404, "not found");
  },
};

// ------------------------------------------------------------------------------------------------ entry point
// The protected resource must be named by its public URL, which is only known per request: build one provider per
// origin (normally exactly one) on first use.
const providers = new Map<string, OAuthProvider<Env>>();

function providerFor(origin: string): OAuthProvider<Env> {
  let p = providers.get(origin);
  if (!p) {
    p = new OAuthProvider<Env>({
      apiRoute: "/mcp",
      apiHandler: mcpApi,
      defaultHandler: site,
      authorizeEndpoint: "/authorize",
      tokenEndpoint: "/token",
      clientRegistrationEndpoint: "/register",
      scopesSupported: [SCOPE],
      requiredScopes: [SCOPE],
      resourceMetadata: { resource: `${origin}/mcp`, authorization_servers: [origin], resource_name: "OpenEVOS (read only)" },
      accessTokenTTL: 3600,
      refreshTokenTTL: 30 * 24 * 3600,
    });
    providers.set(origin, p);
  }
  return p;
}

export default {
  fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    return providerFor(new URL(request.url).origin).fetch(request, env, ctx);
  },
} satisfies ExportedHandler<Env>;
