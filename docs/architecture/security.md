# Security Posture

AgeniusDesk CE is an admin console for managing n8n fleets. The trust model treats an authenticated admin as fully trusted: the security work is concentrated on the pre-authentication surface (the auth gate, edge-header trust, webhook tokens), on preventing secret leakage, and on closing path-traversal and SSRF vectors that an unauthenticated or partially-authenticated caller could reach. The release-hardening pass and its accepted residual risks are recorded in `docs/code-review/security-hardening-findings.md`; this page summarizes the controls actually implemented in code.

See also: [Architecture Overview](overview.md), [Authentication & RBAC](auth.md), [API Reference](api.md), [Module System](modules.md), [Configuration](../CONFIG.md).

## Threat model

- **In scope.** Anyone who can reach the listening port without credentials; a partially-trusted caller (e.g. one that can set request headers); leakage of secrets through API responses, logs, or stored files; path traversal in the static/theme file servers; server-side request forgery via operator-supplied probe URLs; CSRF against the cookie-authenticated browser session.
- **Trusted.** An authenticated admin. Once a request resolves to an admin identity (session, edge, or admin token) it is allowed to do admin things, including operations that are root-equivalent when a Docker socket is mounted. RBAC adds coarse role tiers (viewer/operator/admin) but the console is not a least-privilege multi-tenant system.
- **Out of scope.** Backend isolation for community modules: they run Python in-process with full data and credential access. The frontend is isolated as of v0.3 (a community view runs in a sandboxed `iframe` and reaches the host only through a whitelisted postMessage bridge; see implemented controls). The inspect/scan/consent flow is defense-in-depth on top, not a backend boundary. Also constraining the Docker socket (root-equivalent by design). See accepted risks below.

## Implemented controls

### Internal-API gate (fail-closed)

`require_internal_api_auth` middleware in `backend/main.py` makes `/api/*` private by default. A request to any `/api/` path is rejected with `401` unless it matches one of:

- the public allowlist `_PUBLIC_API_EXACT` (`/api/status`, `/api/health/docker-env`, and the `/api/auth/*` bootstrap endpoints),
- the `/api/v1/` prefix (which enforces its own `X-API-Key`),
- a legacy webhook path with a valid token (below),
- the self-authenticating music trigger,
- the dashboard MCP prefix with a matching bearer token,
- otherwise, a resolved identity from `current_user(request)`.

When login is disabled and `AGD_REQUIRE_AUTH` is false (an explicitly-open install) the gate is a no-op. This is the one easy-to-audit default; individual routers still enforce their own role checks on top.

### Edge-auth trust (opt-in)

Edge identity headers (`Cf-Access-Authenticated-User-Email`, `X-Forwarded-User`) are ignored unless `AGD_TRUST_EDGE_AUTH=true` (`backend/auth_gate.py`, `edge_identity()`). This closes the spoofing vector where a directly-reachable port would otherwise accept a client-supplied identity header. Only enable it when the app is reachable exclusively through a trusted proxy that strips client-supplied identity headers. When trusted, an edge identity resolves to an `admin`-role user.

### X-Forwarded-For trust (opt-in)

Login throttling and client-IP attribution ignore `X-Forwarded-For` unless `AGD_TRUST_FORWARDED_FOR=true` (`backend/modules/auth/service.py`). Without a configured proxy trust boundary, a forwarded IP header could be used to evade lockout or poison IP attribution.

### Legacy webhook token

The legacy machine-ingest endpoints `/api/errors/webhook` and `/api/messages/webhook` stay open by default for backward compatibility with existing n8n handlers. Set `AGD_WEBHOOK_TOKEN` to require a token: the gate's `_legacy_webhook_ok()` accepts the value either in `X-AGD-Webhook-Token` or as `Authorization: Bearer <token>`, compared with `hmac.compare_digest`. New integrations should prefer the `X-API-Key` protected `/api/v1/...` webhooks.

### Path-traversal guards (static JS and themes)

- **JS static route.** `serve_js()` in `main.py` resolves the requested path against the `frontend/js` root, rejects anything that escapes the root (`relative_to` raises), and serves only `.js` files. Used so the import cache-busting rewrite cannot be turned into an arbitrary-file read.
- **Themes.** Theme IDs are validated, theme paths are resolved under known theme roots only, custom theme names are slugified before saving, and a theme must exist before it can be activated (`backend/modules/themes/router.py`).

### Server-side-fetch SSRF guard

When the server fetches an operator-supplied URL, `assert_safe_probe_url()` validates it first. It lives in the shared `backend/net.py` (alongside `tls_verify()`), so every module calls the one guard rather than cross-importing from `assistant`. Loopback and private/LAN ranges stay allowed because self-hosted services (Ollama, MCP servers, Qdrant, LAN n8n) legitimately run there, but it resolves the hostname and blocks the cloud metadata service and link-local space (`169.254.0.0/16`, `fe80::/10`), multicast, and reserved/unspecified addresses. It guards:

- the Ollama URL and the `custom`-provider / RAG Qdrant URLs (model listing, connection test) — on a failed test the fetched body is not reflected, so the probe is not an SSRF read primitive;
- **every MCP management fetch** (add/test/discover/execute), routed through the shared `_normalize_mcp_urls` chokepoint in `backend/modules/assistant/mcp_client.py`;
- **every n8n connect path** (create instance / setup wizard / test-creds), routed through the shared `test_connection_with` chokepoint in `backend/modules/n8n_proxy/client.py`, plus the error-handler install and credential-schema fetches;
- **the knowledge Qdrant source URL** (`backend/modules/knowledge/backends.py`), which also stopped reflecting the target's response body on error.

The 2026-07-01 cross-module review closed the remaining uneven-coverage cases (findings S1/S2/S5/S6/S7) at these shared chokepoints. Outbound TLS verification is likewise centralized: `tls_verify()` (honoring `AGD_TLS_VERIFY`) is threaded through the assistant provider, RAG, and knowledge httpx clients so the flag behaves the same everywhere.

### Agent Fleet code execution is admin-gated

Registering or viewing a vault agent imports and executes operator-authored `graph.py` in-process (full data/credential access, mounted Docker socket), so there is no safe read subset. The whole `/api/agent-fleet` router requires `require_role("admin")` (`backend/modules/agent_fleet/router.py`) rather than merely an authenticated identity.

### Markdown rendering escapes first

LLM/agent/MCP/RAG output can carry attacker-influenced HTML. The hand-rolled markdown renderers (`assistant.js`, `errors.js`, `codelab.js`) HTML-escape before their inline regex transforms and drop non-`http(s)` link hrefs (blocking `javascript:`); the Agent Fleet view sanitizes `marked` output with DOMPurify and fails safe to escaped text if the sanitizer can't load. The shared error item was hardened in v0.4.0.

### CSRF

`csrf_protect` middleware in `main.py` enforces a double-submit check on cookie-authenticated browser mutations: a non-safe method, an internal `/api/` path (not `/api/v1/`), an `agd_session` cookie present, and no bearer/API-key header. The readable `agd_csrf` cookie must match the `X-AGD-CSRF` header or the request is `403`. The auth bootstrap endpoints (`/api/auth/setup|login|login/totp|forgot|reset`) are exempt because no valid session exists yet. The frontend echoes the cookie both in `api()` and via a global `window.fetch` shim (see [Frontend Architecture](frontend.md)).

### RBAC and identity

`current_user()` resolves identity in precedence order: local session cookie, then trusted edge header, then admin token (`AGD_ADMIN_TOKEN`, constant-time compared). Roles are ranked viewer < operator < admin; `require_role(min_role)` builds a dependency that 401s on no identity and 403s on insufficient role, and is a no-op on an open install. It is the single floor primitive across every module (the module manager was moved off the older `require_trusted_request` for parity). See [Authentication & RBAC](auth.md).

Login throttling covers **both factors**: the password step does not reset the per-username+IP lockout counter when TOTP is enabled, and `/api/auth/login/totp` records failed codes and checks the lockout, so a wrong-code loop trips the same lockout as password guessing rather than running unbounded (2026-07-01 cross-module finding S3).

### Secret handling

Secrets are encrypted at rest with Fernet (AES-128-CBC + HMAC-SHA256), keyed from `SECRET_KEY` (env, else persisted `data/.secret_key`, mode 600). The `$NAME` reference convention keeps real values out of config files. Public API keys are stored as `sha256` hashes only, never raw. The dashboard MCP and public-API responses deliberately omit secret values and instance API keys (names/hints only). `harden_file_permissions()` chmods the sensitive data files to 600 on boot.

### Community-module frontend isolation (sandboxed iframe)

A community module's frontend view is not injected into the app page. The loader (`frontend/js/community-modules.js`) mounts each view in an `<iframe sandbox="allow-scripts allow-popups allow-popups-to-escape-sandbox allow-modals allow-downloads">`. The deliberate omission of `allow-same-origin` gives the frame an opaque origin, so the module's script cannot reach the host `window`, DOM, cookies, or storage, and cannot navigate the top frame: a buggy or hostile module can break itself but not the AgeniusDesk UI.

Because the frame is opaque-origin, its own `fetch()` carries no session cookie, so the module reaches the host only through a `postMessage` RPC bridge. The host listens for messages, verifies each one came from that iframe's `contentWindow`, and dispatches a small whitelist: `fetch` (restricted to same-origin `/api/` paths, performed host-side so the session cookie and CSRF token apply), `notify` (toast), `navigate`, and `openInHarness`. Any other path or method is rejected. The bridge reimplements `window.AgeniusDesk` inside the iframe over this channel, so module code keeps the same API. The host also pushes the active theme's CSS variables into the iframe and auto-resizes it to content height. The backend half of a module still runs in-process (see accepted risks); the iframe constrains the frontend only.

### Other response-level controls

- `security_headers` middleware sets `X-Content-Type-Options: nosniff`, `X-Frame-Options: SAMEORIGIN`, `Referrer-Policy: strict-origin-when-cross-origin`, HSTS over HTTPS, and an opt-in CSP (`AGD_CSP`, off by default because the CDN editors and media embeds need a deliberate policy).
- `limit_request_size` rejects bodies over `AGD_MAX_REQUEST_BYTES` (default 25 MiB) by `Content-Length` before reading them.
- CORS origins are restrictable via `AGD_CORS_ORIGINS` (default `*`).

## Security knobs

| Env var | Default | Effect |
|---|---|---|
| `AGD_DISABLE_LOGIN` | `false` | Disable browser login entirely. Logged loudly. Dev/localhost only |
| `AGD_REQUIRE_AUTH` | `false` | Hard gate for token/edge-fronted installs |
| `AGD_TRUST_EDGE_AUTH` | `false` | Trust `Cf-Access-Authenticated-User-Email` / `X-Forwarded-User` |
| `AGD_TRUST_FORWARDED_FOR` | `false` | Trust `X-Forwarded-For` for login IP / throttling |
| `AGD_ADMIN_TOKEN` | empty | Break-glass / automation bearer token (admin role) |
| `AGD_WEBHOOK_TOKEN` | empty | Require a token on the legacy webhook endpoints |
| `DASHBOARD_MCP_TOKEN` | empty | Bearer token for `/api/mcp-dashboard` non-browser clients |
| `DASHBOARD_MCP_ALLOWED_HOSTS` | `dashboard:3000,localhost:3000,127.0.0.1:3000,localhost,127.0.0.1` | FastMCP DNS-rebinding Host allowlist |
| `AGD_CSP` | empty | Content-Security-Policy header value (opt-in) |
| `AGD_CORS_ORIGINS` | `*` | Allowed CORS origins |
| `AGD_MAX_REQUEST_BYTES` | `26214400` | Max request body size |
| `SECRET_KEY` | persisted/generated | Master key for Fernet encryption |

Login policy knobs (`AGD_LOGIN_MAX_ATTEMPTS`, `AGD_LOGIN_LOCKOUT_MINUTES`, the `AGD_PASSWORD_*` composition rules, session TTLs) are in `backend/config.py` and documented in [Configuration](../CONFIG.md).

## Accepted risks

| Risk | Posture |
|---|---|
| Docker socket access | Root-equivalent by design. Only mount the socket where every console user is a fully-trusted admin |
| Community modules | Backend accepted, frontend sandboxed. The backend runs Python in-process (full data and credential access); install only from sources you trust. The frontend is isolated: a community view runs in an `iframe` with `allow-scripts` but NOT `allow-same-origin`, so it cannot read or change the host DOM, `window`, cookies, or storage, and it reaches the host only through a postMessage bridge whitelisted to same-origin `/api/` fetches (auth and CSRF added host-side), `notify`, `navigate`, and `openInHarness`. On top of that, a two-phase inspect/install flow runs a static AST scan and requires consent proportional to severity (CRITICAL: type the id, HIGH: acknowledge) and records an audit row; a static scan is a heuristic, not a boundary, and cannot follow obfuscation, runtime-fetched code, or dynamic imports. Out-of-process backend isolation is the remaining deferred boundary. See [Module System](modules.md) |
| Regression coverage | A `pytest` suite (~280 tests) now covers the security-relevant paths: router RBAC floors, edge/auth trust, webhook tokens, traversal, the SSRF guard, error-item XSS, the four 2026-07-01 High fixes, and the cross-module remediation (shared SSRF/TLS guard, second-factor throttle, module-manager floor) (`tests/test_router_rbac.py`, `test_security_hardening.py`, `test_assistant_authz_ssrf.py`, `test_high_severity_fixes.py`, `test_error_item_xss.py`, `test_review_medium_low.py`, `test_cross_module_review.py`) |
| Legacy `enc:` secret format | Unauthenticated XOR-stream, kept only for decryption/migration; re-save migrates to Fernet |

## Deployment hardening checklist (public bind)

| Step | Action |
|---|---|
| Login | Keep `AGD_DISABLE_LOGIN=false`; create the owner account on first visit |
| Edge proxy | Front the app with a TLS-terminating proxy; only set `AGD_TRUST_EDGE_AUTH=true` if it strips client-supplied identity headers |
| Forwarded IP | Set `AGD_TRUST_FORWARDED_FOR=true` only behind a proxy that controls `X-Forwarded-For` |
| Webhooks | Set `AGD_WEBHOOK_TOKEN` if the legacy webhook endpoints are exposed; otherwise migrate to `/api/v1/*` keys |
| MCP | Set `DASHBOARD_MCP_TOKEN` before exposing `/api/mcp-dashboard` to non-browser clients |
| CORS | Set `AGD_CORS_ORIGINS` to your exact origins instead of `*` |
| CSP | Set `AGD_CSP` once you have validated a policy that allows the CDN editors and media embeds |
| Secret key | Back up `data/.secret_key` with the data volume; set `SECRET_KEY` explicitly if you manage keys externally |
| Docker socket | Do not mount it unless every user is a trusted admin |
| Modules | Install community modules only from sources you trust |
