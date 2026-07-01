# Spec: `http.request` host bridge capability

Status: **CORE LANDED (2026-07-01)** on branch `feat/http-request-bridge`, behind the
existing isolation flag. Shipped: the `host.http` capability model
(`HttpAuth`/`HttpEndpoint`/`HttpBridgeCapability` in `module_registry.py`), the grant
extension + `POST /api/_host/http/request` route in `bridge.py` (endpoint + method
gating, relative-path validation + host-lock, host-side auth injection for
`bearer`/`header`/`basic`/`query`, worker-header sanitation, redirects-off, response
size cap + `truncated`, response-header allowlist, resolve-once-at-mint pins + a
DNS-rebind pre-flight guard), the scanner rule (undeclared http.request → HIGH,
declared → INFO, `verify_tls:false` + mutating methods surfaced), and `tests/
test_http_bridge.py` (16 tests). Full suite green (285).

**Deferred (NOT in this increment, tracked below):** operator-override of `base_url`
at install + endpoint config *revisions* (endpoints are minted from the live manifest
at spawn, same as the existing `write_paths`/`host.assistant` grants — the install-time
override + revision store is the same host work as the consented-secret grant store,
isolation-spec phase 9); **connection-level** IP pinning via a custom transport (the
current guard is a resolve-once-at-mint + per-call re-resolve pre-flight check, which
covers hostname rebinding; literal-IP base URLs — the homelab norm — have no DNS to
rebind); streaming responses (§8, the HA prereq); operator-added dynamic endpoints
(§9 v2); per-module rate limiting (§8). The module-side dual-mode `_host.http_request`
SDK lives in the community-modules repo, not here.

Date: 2026-06-28 (revised 2026-07-01: locked the endpoint model against the first
consumers — dynamic endpoints deferred to v2, stateful/session auth ruled a
non-goal, response-header allowlist locked; added the Consumers section; **then
landed the core bridge**, see Status above).

Related: `2026-06-28-community-module-candidates.md` (why this is the highest-leverage
host investment), `2026-06-27-out-of-process-backend-isolation.md` (the isolation
tiers + bridge this extends), `backend/modules/_runtime/bridge.py` (the surface to
extend). **Consumers grounding this spec:**
`2026-06-30-proxmox-module.md` (flagship; the `PVEAPIToken` header, `verify_tls:false`,
and the one-cluster-per-install finding that drove the endpoint-model lock below),
`2026-06-30-support-ticketing-module.md` (reply-via-REST mail endpoint, a mutating
POST), `2026-06-30-consented-secret-tier.md` (the complement: held credentials for
the *non*-HTTP protocols this bridge deliberately does not cover).

## 1. Problem

A community module that talks to an external service (Proxmox, Home Assistant,
Cloudflare, NocoDB, Qdrant, object storage, Uptime Kuma) needs a **credential** to
do it. The bridge today exposes only `notes.*` and `assistant.complete`; under the
`subprocess` and `container` isolation tiers the worker env is **scrubbed** of
secrets and host imports are blocked. So a credential-holding module has exactly one
place to run: `in_process` — with full host access and no containment.

The result: the entire credential-holding REST quadrant (and therefore most of the
Homelab Pack) can be *built* but cannot be *isolated*. This spec closes that gap with
one new bridge namespace, mirroring the shape of `assistant.complete`: **the
credential never enters the worker; the host makes the authenticated call.**

## 2. Design principles

1. **The host owns the credential, the base URL, and the host pin.** The worker
   supplies an *endpoint id* + a *relative path* + method/query/headers/body. It
   never sees the secret, never sets the target host, never sets the base URL.
2. **Operator-consented endpoints.** What a module may call is declared in its
   manifest and surfaced at install. The grant is built from the declaration, not
   from anything the worker sends at runtime.
3. **Identical code across tiers (dual-mode).** A module always calls
   `host.http.request(...)`. In `in_process` the host client resolves the secret and
   calls directly; under isolation the same call goes over the bridge. Same as the
   `youtube-research` dual-mode pattern for `assistant.complete`. A useful
   side-effect: even `in_process` modules stop reading the secret store directly,
   shrinking their capability surface and their scanner findings.
4. **Read-first.** Default allowed methods are `["GET","HEAD"]`; a monitoring module
   cannot mutate even if compromised. Mutating methods are an explicit per-endpoint
   opt-in with their own consent step — a module never gets write access by default.
5. **Pin, don't follow DNS.** The host resolves each endpoint's host once and pins
   the resolved IP set to the endpoint config revision. A later IP change is not
   silently followed; it requires explicit operator refresh/re-approval. This blocks
   DNS-rebinding while staying homelab-compatible.

## 3. Capability declaration

Extends the existing `host` capability block (which already carries `assistant` and
`broadcast`, read by `bridge.mint()`):

```json
"capabilities": {
  "network": { "enabled": true, "hosts": ["api.cloudflare.com"] },
  "host": {
    "http": {
      "enabled": true,
      "endpoints": [
        {
          "id": "cloudflare",
          "base_url": "https://api.cloudflare.com/client/v4",
          "auth": { "type": "bearer", "secret_ref": "CLOUDFLARE_TOKEN" },
          "methods": ["GET", "POST", "PUT", "DELETE"],
          "verify_tls": true
        },
        {
          "id": "proxmox",
          "base_url": "https://10.10.0.20:8006/api2/json",
          "auth": {
            "type": "header",
            "header": "Authorization",
            "secret_ref": "PROXMOX_TOKEN",
            "format": "PVEAPIToken={value}"
          },
          "methods": ["GET", "POST"],
          "verify_tls": false
        }
      ]
    }
  }
}
```

Endpoint fields:

| Field | Required | Meaning |
|---|---|---|
| `id` | yes | Worker-facing name. Unique within the module. |
| `base_url` | yes | Scheme + host + optional base path. **Manifest value is a default *suggestion* only.** The effective `base_url` is stored host-side per install/environment and is operator-overridable at install (homelab hosts/IPs are deployment-specific). The **host pin** derives from the *effective* value; the worker cannot change it. |
| `auth` | no | How the host injects credentials (below). Absent = unauthenticated call. |
| `methods` | no | Allowed HTTP methods. **Default `["GET","HEAD"]` (read-only).** Any mutating method (`POST`/`PUT`/`PATCH`/`DELETE`) is **opt-in per endpoint** and triggers separate consent text at install (see §6). |
| `verify_tls` | no | Default `true`. `false` for self-signed LAN services (Proxmox, many homelab boxes). |

**Effective config is host-side, not manifest-trusted.** At install the operator
confirms (or overrides) each endpoint's `base_url` and supplies the `secret_ref`
value. The host persists the effective `{base_url, methods, verify_tls, pinned IP
set}` per install as an **endpoint config revision**. The manifest only proposes
defaults; it is never the runtime source of truth for what a module may reach.

`auth` shapes:

| `type` | Behaviour |
|---|---|
| `bearer` | `Authorization: Bearer {value}` |
| `header` | sets `{header}` to `{format}` (default `{value}`), e.g. Proxmox `PVEAPIToken={value}` |
| `basic` | `Authorization: Basic base64(user:value)`; `user` from a second ref or literal |
| `query` | appends `{param}={value}` to the query string |

`{value}` is the resolved secret. `secret_ref` is a key in the secrets store; it is
**resolved host-side at call time** (so rotation works and the value is never held
longer than the call). Manifests store key names only, never values — unchanged
policy.

## 4. Wire contract (bridge surface)

New route on `bridge_app` in `backend/modules/_runtime/bridge.py`. Gated by the same
per-spawn bearer token + `_require_grant` dependency (cookies rejected — not a
browser surface).

`POST /api/_host/http/request`

```json
{
  "endpoint": "cloudflare",
  "method": "GET",
  "path": "/zones",
  "query": { "per_page": "50" },
  "headers": { "Accept": "application/json" },
  "body": null
}
```

Response:

```json
{
  "status": 200,
  "headers": { "content-type": "application/json" },
  "body": "...",
  "truncated": false
}
```

- `body` is a string (JSON or text). Binary responses are base64 with a
  `content_encoding: "base64"` flag. Non-2xx is returned as-is (status + body), not
  raised — the module decides how to handle it.
- `truncated: true` when the response hit the size cap (below).

## 5. Host-side enforcement

`mint()` extends the `BridgeGrant` with the resolved endpoint set
(`http_endpoints: dict[id -> EndpointGrant]`), built from the manifest at spawn.
Secrets are **not** baked into the grant; only `secret_ref` names are, resolved per
call.

On each `http/request` the host:

1. **Looks up the endpoint** in the grant, using the **effective config revision**
   (host-side `base_url`/`methods`/`verify_tls`/pinned IPs), not manifest values.
   Unknown id → 403.
2. **Checks the method** against the endpoint's effective `methods`. Disallowed →
   403. (Mutating methods are present only if the operator opted in at install.)
3. **Builds the URL** = effective `base_url` + `path`. The `path` is validated like a
   vault path: reject `..`, reject a scheme/`//host` prefix, reject embedded `@`,
   backslashes, NUL. The final URL's scheme+host+port **must equal** the effective
   `base_url`'s — the worker cannot pivot off the consented host.
4. **Resolves auth** from `secret_ref` via `load_secrets()` and injects it. The
   worker-supplied `headers` are sanitised first: it may not set `Authorization`,
   `Host`, `Cookie`, or any hop-by-hop header; the injected auth always wins.
5. **Connects to a pinned IP.** The call targets one of the endpoint's
   pinned IPs (resolved once at config time), not a fresh DNS lookup, with the
   `Host`/SNI set to the consented hostname. If the host no longer resolves to any
   pinned IP, the call fails closed with a "re-approve endpoint" error rather than
   following the new address.
6. **Makes the call** with httpx: `verify=endpoint.verify_tls`, a fixed connect/read
   **timeout** (e.g. 30s), **redirects disabled** (a 3xx is returned to the module
   as-is; following it could leak the credential to a redirected host).
7. **Caps the response**: max body bytes (e.g. 5 MB) → `truncated`. Echoes only the
   **response-header allowlist** (§9 resolved #5); `set-cookie`, `www-authenticate`,
   and the injected auth headers are always stripped, never returned to the worker.
8. Returns `{status, headers, body, truncated}`. **Credentials never appear** in the
   returned request echo or response.

### SSRF posture (deliberately homelab-aware)

Standard SSRF defense blocks private IPs. We **cannot** — a Proxmox box at
`10.10.0.20` or a Home Assistant at a LAN address is the whole point. Safety comes
from a different invariant: **the host is pinned to the operator-consented
`base_url`**, and the worker only supplies a relative path. The worker therefore
cannot reach anything the operator did not explicitly approve at install — including
the bridge's own loopback port or host metadata services. DNS-rebinding is closed by
**resolve-once-and-pin** (step 5): the endpoint's IP set is fixed at config time and
an IP change fails closed pending re-approval, so a rebind cannot redirect a call to
a new address mid-flight.

## 6. Consent + scanner integration

- **Consent modal** surfaces each endpoint at install: the effective `base_url`
  (operator-confirmed or overridden), which `secret_ref` it uses, allowed `methods`,
  and `verify_tls` state. Read-only endpoints get the baseline consent line ("this
  module will read from `api.cloudflare.com` using your `CLOUDFLARE_TOKEN`").
- **Mutating methods get separate, stronger consent text.** If an endpoint requests
  any of `POST/PUT/PATCH/DELETE`, the modal shows a distinct warning block ("this
  module can **change** data on `…` — create/update/delete") that the operator
  acknowledges separately from the read grant.
- **Re-consent on host change.** Changing an endpoint's effective `base_url` host (at
  install or later) starts a new endpoint config revision and re-prompts consent;
  the old pinned IPs and grant are discarded. An IP-only change (same host, new
  address) also requires the explicit refresh/re-approve from step 5.
- **Scanner** (`scanner.py`): calling the http bridge **without** declaring
  `host.http` → HIGH (mirrors the existing `assistant.complete` rule). Declared use
  → INFO (transparency). An endpoint with `verify_tls:false` → INFO note in the
  report so it is visible, not hidden.

## 7. What it unlocks

Every credential-holding REST module moves from `in_process`-only to
**safe-under-isolation** on one pattern: Cloudflare, NocoDB/Baserow/Airtable,
Qdrant, object storage (REST/SigV4), Uptime Kuma, **Proxmox**, **Home Assistant**,
reverse proxy, Pi-hole/AdGuard, Tailscale/NetBird, TrueNAS. This is the single host
change that makes the Homelab Pack shippable as properly-isolated community modules.

### 7.1 Consumers (what building these confirmed)

Three consumer specs are now written against this bridge; each validated a piece of
the contract and pushed one thing back:

- **Proxmox** (`2026-06-30-proxmox-module.md`) — the flagship. Confirmed the `header`
  auth type with `format: "PVEAPIToken={value}"` and `verify_tls:false` map cleanly.
  Pushed back **two findings** now locked here: (a) the endpoint model is *one target
  per declared endpoint per install* — the "add many clusters at runtime like n8n
  instances" vision needs dynamic endpoints (§9, deferred to v2); (b) Proxmox realm
  username/password login (ticket → cookie + CSRF) does **not** fit this bridge,
  which crystallized the **stateful-auth non-goal** (§9).
- **Support / ticketing** (`2026-06-30-support-ticketing-module.md`) — its reply path
  option 2 is a `mail` endpoint using `bearer` auth with a mutating `POST`, exercising
  the mutating-endpoint consent (§6). Confirms the read/write split works for a
  send-only endpoint.
- **Consented-secret tier** (`2026-06-30-consented-secret-tier.md`) — the deliberate
  complement. This bridge covers credentials the **host can use on the module's
  behalf over HTTP**. Credentials that must live **in the worker** (IMAP/SMTP/Redis/
  Postgres wire, or an in-worker keyed library) are that tier's job, not this
  bridge's. The two together cover the credential space; a module picks per-credential
  (Proxmox: token via this bridge; a Redis monitor: AUTH password via the tier).

## 8. Phasing

- **v1:** request/response only (the contract above). Covers all polling/REST use.
  Endpoints are manifest-declared, one effective target per endpoint per install (§9).
- **Deferred — streaming responses (SSE/chunked).** This is the **Home Assistant
  prereq**: HA's live WebSocket state cannot be relayed under isolation until a
  streaming bridge exists, so the HA module polls REST under isolation (or holds the
  WS only when `in_process`) in the meantime. Tracked as its own future spec, not a
  revision of this one.
- **Deferred — dynamic / operator-added endpoints (v2, §9).** The Proxmox "one
  cluster per install" limit is lifted by endpoint *instances* under a declared
  endpoint *template*; specced at a high level in §9, built when a second consumer
  needs many targets of one kind.
- **Deferred:** per-module rate limiting / quota on the bridge.

## 9. Resolved calls + remaining questions

**Resolved (locked for build):**

1. **Default methods** — read-only (`GET`/`HEAD`). Mutating methods
   (`POST`/`PUT`/`PATCH`/`DELETE`) are per-endpoint opt-in with separate consent
   text (§3, §6).
2. **`base_url` ownership** — operator-overridable. Manifest value is a default
   suggestion; the effective `base_url` is stored host-side per install/environment
   as an endpoint config revision; changing the host re-prompts consent (§3, §6).
3. **DNS rebinding** — resolve-once-and-pin per endpoint config revision; an IP
   change fails closed pending explicit refresh/re-approval, never silently followed
   (principle 5, §5 step 5).
4. **Endpoint cardinality — one target per declared endpoint per install (v1)**
   *(locked 2026-07-01, driven by Proxmox §4).* A manifest declares a fixed set of
   endpoints; each has exactly one effective target per install (the operator sets
   its `base_url` + `secret_ref` value). A module that manages N targets of one kind
   (N Proxmox clusters, N Cloudflare accounts) does **one install per target** in v1.
   The worker names only the endpoint `id`. This keeps the grant, the pin, and the
   consent one-to-one with a declared endpoint — the simplest thing that is safe.
5. **Response headers — echo an allowlist, drop the rest** *(locked 2026-07-01)*.
   Matching the env-scrub philosophy, the bridge echoes only a safe allowlist back to
   the worker: `content-type`, `content-length`, `content-encoding`, `etag`,
   `last-modified`, `retry-after`, and the `ratelimit-*` / `x-ratelimit-*` family.
   Everything else is dropped; `set-cookie`, `www-authenticate`, and the injected
   auth headers are **always** stripped (never echoed), as §5 step 7 already requires.
6. **Stateful / session auth is a NON-GOAL** *(locked 2026-07-01, driven by Proxmox
   §3).* The bridge injects **static, per-call** auth only (`bearer`/`header`/`basic`/
   `query`, §3). It does **not** perform login round-trips that mint a session and
   carry it forward: Proxmox `/access/ticket` → `PVEAuthCookie` + `CSRFPreventionToken`,
   OAuth2 authorization-code / refresh-token dances, or anything requiring the bridge
   to hold, refresh, and CSRF-echo session state. Those would turn the bridge into a
   stateful auth client — a much larger surface and a different trust model. A module
   needing them uses a **static API token** instead (the norm for Proxmox, Cloudflare,
   HA, Pi-hole, etc.) or runs `in_process`. OAuth2 *client-credentials* (a POST to a
   token endpoint for a short-lived bearer) is the one plausible future exception and
   is noted below, not in v1.

**Deferred to v2 — dynamic / operator-added endpoints (the shape).** Lift the
one-per-install limit (resolved #4) with **endpoint instances under a declared
endpoint template**: the manifest declares a *template* (the auth shape, allowed
`methods`, `verify_tls` default, and a human label like "Proxmox cluster"); the
operator **adds instances at runtime**, each its own `{base_url, secret_ref, pinned
IP set, consent}` running the exact same endpoint-config-revision + pin + consent
machinery as a declared endpoint does today. The worker then names **both** the
endpoint (template) id **and** an instance id in the request. This is the clean path
to "add a Proxmox cluster like you add an n8n instance" without weakening the
per-target grant/pin/consent invariants. Build it when a second consumer needs many
targets of one kind; until then, one install per target (resolved #4).

**Still open:**

- **Multiple credentials per endpoint** (e.g. `basic` auth user + pass as two refs, or
  a body-signing key alongside a header token). Today `auth` resolves one `secret_ref`.
- **OAuth2 client-credentials token exchange** as a future auth `type` (bridge does the
  token POST, caches the short-lived bearer host-side, injects it). Plausible extension
  of the static-auth model without the full stateful-session surface non-goal #6 rules
  out. Not v1.
- **Pinned-IP refresh UX:** where the "re-approve endpoint" action lives (module
  manager vs a Fleet-Health-style banner) when a pinned host's address changes.

## 10. Testing

- Unit: URL building rejects `..`, absolute URLs, `@`, host-change attempts; method
  gate (default GET/HEAD only; a mutating method 403s unless the effective config
  opted in); header sanitation drops `Authorization`/`Host`/`Cookie`; auth injection
  per `type`; response size cap + truncation; redirect returned-not-followed.
- Unit: the response-header **allowlist** (§9 #5) — an allowed header (e.g.
  `retry-after`, `x-ratelimit-remaining`) is echoed; a non-listed header and
  `set-cookie`/`www-authenticate`/the injected auth header are all dropped.
- Unit: connection targets a pinned IP; a host that resolves to a non-pinned IP fails
  closed with the re-approve error rather than connecting to the new address.
- Unit: effective config (not manifest) is the runtime source of truth — a manifest
  `base_url`/`methods` override is ignored in favour of the stored install revision.
- Integration: a dual-mode reference module (extend `youtube-research` or a small
  Cloudflare/Proxmox stub) makes the same call `in_process` and under `subprocess`
  and `container`, asserting identical results and that the secret never appears in
  the worker env or the returned echo.
- Regression: scanner flags undeclared bridge use HIGH; declared use INFO;
  `verify_tls:false` surfaced.
