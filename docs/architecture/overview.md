# Architecture Overview

AgeniusDesk CE is a self-hosted control plane for managing one or more n8n instances. It is a single FastAPI application (`backend.main:app`) that serves a zero-build vanilla-JS frontend, exposes an internal `/api/*` surface plus a versioned public `/api/v1/*` API, brokers real-time updates over a WebSocket, and persists state in a single SQLite file plus a small set of encrypted JSON files on disk. The whole thing runs as one container; the only optional companion is the host Docker socket (for the Containers tab). This page is the system model: the app object, its startup sequence, the middleware stack, module auto-discovery, the data and broadcast layers, static serving, and the request lifecycle. Each subsystem links out to its own page.

## Component model

```
                          browser
                            |
            HTTP /api/*, /api/v1/*, /  +  WS /ws
                            |
         +------------------v-------------------+
         |        FastAPI app (backend.main)    |
         |                                      |
         |  middleware (outer -> inner):        |
         |    CORSMiddleware                    |
         |    no_cache_static                   |
         |    csrf_protect                      |
         |    security_headers                  |
         |    limit_request_size                |
         |    require_internal_api_auth         |
         |                                      |
         |  +--------------------------------+  |
         |  | module routers (auto-mounted)  |  |
         |  |  admin, assistant, errors,     |  |
         |  |  messages, n8n_proxy, health,  |  |
         |  |  docker_mgr, knowledge, notes, |  |
         |  |  player, themes, webhooks,     |  |
         |  |  modules, auth, insights,      |  |
         |  |  public_api, dashboard_mcp ... |  |
         |  +--------------------------------+  |
         |                                      |
         |  /api/v1  -> mounted sub-app         |
         |  /api/mcp-dashboard -> FastMCP app   |
         |  /        -> StaticFiles(frontend/)  |
         +---+----------------+-----------------+
             |                |
        ConnectionManager   get_db() singleton
        (WS broadcast bus)  aiosqlite -> data/dashboard.db
             |                |
          all WS clients   data/ (config.json, secrets.json,
                            .secret_key, users.json, ...)
                                     |
                         optional: /var/run/docker.sock
                         upstream: n8n instances (HTTP API)
```

The FastAPI object is created once in `backend/main.py`:

```python
app = FastAPI(title="AgeniusDesk", version="0.1.0", lifespan=lifespan)
```

## Startup sequence (lifespan)

Startup logic lives in the `lifespan` async context manager in `backend/main.py`. FastAPI runs it once before serving and tears it down on shutdown. Steps, in order:

| Step | Call | Purpose |
|---|---|---|
| 1 | `await get_db()` | Open the aiosqlite singleton, run `_init_tables()` then `_migrate()` |
| 2 | `harden_file_permissions()` | Best-effort `chmod 600` on `data/` secrets, config, DB, key files |
| 3 | `migrate_inline_to_secrets()` | Move any inline credentials in config into the encrypted secrets store |
| 4 | `apply_overlay_to_settings(...)` | Apply `config_overlay` (runtime setting overrides) onto `settings` |
| 5 | resolve `dashboard_mcp` session manager | Get FastMCP's `session_manager` if the dashboard MCP server imports cleanly |
| 6 | `await ensure_baseline()` | Seed the assistant "constitution" / baseline-instruction file if absent |
| 7 | log + auth-posture warnings | Warn loudly if `AGD_DISABLE_LOGIN` is set; note edge-auth behavior |
| 8 | enter MCP `session_manager.run()` (if present) then `yield` | FastMCP streamable-HTTP needs its task group driven from the host app, because mounted sub-app lifespans are not invoked by FastAPI |

Every step except `get_db()` is wrapped in try/except and logged on failure, so a broken optional subsystem (overlay, MCP, baseline) degrades rather than aborting boot. Shutdown calls `await close_db()`.

Module registration and the sub-app mounts happen at import time, after `app` is constructed but before the static mount (see [Module System](modules.md)). They are not inside `lifespan`.

## Middleware stack

Middleware is registered with `app.add_middleware` / `@app.middleware("http")`. Starlette runs middleware in reverse registration order on the way in, so the last-registered runs first (outermost). Registration order in `backend/main.py` is: CORS, then `require_internal_api_auth`, `limit_request_size`, `security_headers`, `csrf_protect`, `no_cache_static`. Effective inbound order (outer to inner):

| Order | Middleware | Behavior |
|---|---|---|
| 1 (outermost) | `CORSMiddleware` | Allow-origins from `AGD_CORS_ORIGINS` (`*` default, or comma list); all methods/headers |
| 2 | `no_cache_static` | On response: sets `Cache-Control: no-cache, must-revalidate` for `.js`/`.css`/`.html`, `/`, `/js/`, `/css/` |
| 3 | `csrf_protect` | Double-submit CSRF check; see below |
| 4 | `security_headers` | On response: `X-Content-Type-Options`, `X-Frame-Options: SAMEORIGIN`, `Referrer-Policy`; HSTS when proto is https; CSP only if `AGD_CSP` set |
| 5 | `limit_request_size` | Reject `Content-Length > AGD_MAX_REQUEST_BYTES` (default 25 MiB) with 413 before reading the body |
| 6 (innermost) | `require_internal_api_auth` | The default-private gate for `/api/*` |

Three are response-shapers (`no_cache_static`, `security_headers`) or pre-body guards (`limit_request_size`); the two that actually gate a request are the auth gate and CSRF.

### `require_internal_api_auth` (default-private gate)

Logic in `backend/main.py`. Non-`/api/` paths pass through untouched. For `/api/*`:

- Allow-listed public paths pass: `_PUBLIC_API_EXACT` (`/api/status`, `/api/health/docker-env`, and the `/api/auth/*` bootstrap endpoints) and the `/api/v1/` prefix (the public API authenticates itself with `X-API-Key`).
- `_LEGACY_WEBHOOK_EXACT` (`/api/errors/webhook`, `/api/messages/webhook`) pass only if `_legacy_webhook_ok` holds: when `AGD_WEBHOOK_TOKEN` is unset they stay open for backward compatibility; when set, the request must present it via `X-AGD-Webhook-Token` or a bearer token (constant-time compared).
- `_SELF_AUTHENTICATING_EXACT` (`/api/music/triggers/fire`) passes (it carries its own auth).
- `/api/mcp-dashboard` passes if it carries the matching `DASHBOARD_MCP_TOKEN` bearer.
- Otherwise: if neither local login nor `AGD_REQUIRE_AUTH` is in force, pass (open install); else require `current_user(request)` to resolve (local session cookie or trusted edge identity), returning 401 if not.

This gives one auditable default: `/api/*` is private unless explicitly listed. Individual routers still enforce their own role checks on top (e.g. `require_trusted_request`). See [Authentication & RBAC](auth.md).

### `csrf_protect` (double-submit)

Enforced only for cookie-authenticated browser mutations: a non-safe method, an internal `/api/` path that is not `/api/v1/`, an `agd_session` cookie present, and no `Authorization: Bearer` / `X-API-Key` header. The auth bootstrap endpoints (`/api/auth/setup|login|login/totp|forgot|reset`) are exempt because no valid session exists yet. On a missing or mismatched `CSRF_COOKIE` vs `CSRF_HEADER` it returns 403. Bearer/API-key callers and edge-only requests are not cookie-CSRF exposed and are skipped. See [Security](security.md).

## Module auto-discovery and router mounting

Modules self-register. `register_modules(app)` (`backend/modules/__init__.py`) is called once at import time in `backend/main.py`:

```python
modules = register_modules(app)
```

It scans `backend/modules/{id}/` (built-ins) then `data/modules/{id}/` (community), loads each `manifest.json`, gates on `min_app_version`, imports the package, and calls `app.include_router(mod.router)` when the module exposes a `router`. Failures are recorded in the registry with a status rather than crashing the app. Full mechanics, the manifest schema, and how to add a module are in [Module System](modules.md).

Two non-router surfaces are mounted as separate ASGI apps after `register_modules`:

- `/api/v1` -> a dedicated `FastAPI` sub-app holding `public_api.router`, giving isolated Swagger docs at `/api/v1/docs`. Starlette strips the mount prefix, so v1 handlers use bare paths.
- `/api/mcp-dashboard` -> the FastMCP streamable-HTTP Starlette app via `dashboard_mcp.server.mount_on(app)`.

## SQLite singleton

`backend/database.py` holds a single module-level `aiosqlite.Connection` returned by `get_db()`. First call creates `data/` if needed, connects to `data/dashboard.db`, sets `row_factory = aiosqlite.Row`, runs `_init_tables()` (the `CREATE TABLE IF NOT EXISTS` set) then `_migrate()` (idempotent PRAGMA-guarded ALTERs, plus dropping tables for features stripped from CE such as `langgraph_runs` and `research_jobs`). Every later caller awaits the same connection object. `close_db()` closes it on shutdown. The full table list is in [Data Model](data-model.md).

## WebSocket broadcast bus

`backend/websocket.py` defines `ConnectionManager` and a process-wide singleton `manager`. It holds a plain list of accepted `WebSocket` connections and exposes:

- `connect(ws)` / `disconnect(ws)` - membership management.
- `broadcast(event, data)` - JSON-encodes `{"event": event, "data": data}` and sends to every client, dropping any socket that raises on send.
- `count` - live connection count (surfaced in `/api/status` as `websocket_clients`).

The `/ws` endpoint in `backend/main.py` gates the upgrade the same way as the HTTP boundary: when login is enforced it requires a valid `agd_session` cookie or a trusted edge identity (browsers cannot set an `Authorization` header on a WS handshake, so token-only mode does not gate `/ws`), then loops on `receive_text()` to keep the socket alive.

Event names broadcast by the codebase:

| Event | Emitted by | Payload |
|---|---|---|
| `error` | `errors` module on inbound error webhook/sync | the stored error row |
| `message` | `messages` module on inbound notification webhook | the stored message row |

The frontend dispatches on `event` to render the live error feed and toast notifications. See [Frontend](frontend.md).

## Static frontend serving and cache-busting

The frontend is plain ES modules with no build step, served from `frontend/`. Three pieces in `backend/main.py` handle it:

- `BUILD_ID = str(int(time.time()))` is computed once per process start.
- `GET /` and `/index.html` read `frontend/index.html` and rewrite the entry script tag to `src="/js/app.js?v=BUILD_ID"`.
- `GET /js/{full_path:path}` (`serve_js`) resolves the request inside `frontend/js`, rejects path traversal and non-`.js` files, then rewrites every relative `.js` import in the file body to append `?v=BUILD_ID` (`_bust_imports` via the `_IMPORT_RE` regex). This forces the whole module graph to reload on a new deploy, which is necessary because some browsers aggressively cache ES modules.
- `app.mount("/", StaticFiles(directory=frontend, html=False))` is registered last, serving everything else (CSS, assets). It must be last because a `/` mount is a catch-all.

The `no_cache_static` middleware additionally stamps `Cache-Control: no-cache, must-revalidate` on these responses.

## Request lifecycle for a typical `/api` call

Example: an authenticated browser does `POST /api/admin/secrets`.

1. `CORSMiddleware` validates the origin and handles preflight.
2. `no_cache_static` (response phase only) will later stamp cache headers; the path is not a static asset so nothing changes.
3. `csrf_protect` sees a non-safe method on an internal `/api/` path with an `agd_session` cookie and no bearer/API-key, so it compares the CSRF cookie against the `X-CSRF-Token` header; mismatch -> 403.
4. `security_headers` will add baseline headers to the eventual response.
5. `limit_request_size` checks `Content-Length` against `AGD_MAX_REQUEST_BYTES`; oversize -> 413.
6. `require_internal_api_auth` sees a non-public `/api/` path, resolves `current_user(request)` from the session cookie or edge identity; unauthenticated -> 401.
7. The request reaches the `admin` module router, whose own dependency enforces the required role.
8. The handler reads/writes via `get_db()` and the encrypted config helpers, optionally calls `manager.broadcast(...)` to push a live update, and returns JSON.
9. On the way out, `security_headers` and `no_cache_static` annotate the response.

A machine call to `POST /api/v1/...` instead skips the cookie-CSRF path entirely, is allow-listed past the internal auth gate by its `/api/v1/` prefix, and is authenticated inside the v1 sub-app by `X-API-Key`. See [API Reference](api.md).

## Container and compose mapping

`docker-compose.yml` defines a single service.

| Compose element | Maps to |
|---|---|
| `dashboard` service (`build: .`) | The FastAPI app from `Dockerfile` (`python:3.12-slim`, `pip install '.[assistant]'`, `uvicorn backend.main:app` on 3000) |
| `${PORT:-3000}:3000` | Host port -> container 3000 |
| `env_file: .env` (not required) | Provider keys and `AGD_*` toggles read by `backend/config.py` |
| `extra_hosts: host.docker.internal:host-gateway` | Lets the dashboard reach host-published services (e.g. an n8n container) on Linux |
| volume `dashboard-data:/app/data` | The entire persisted state: SQLite DB, encrypted config/secrets, `.secret_key`, users, community modules, templates |
| `/var/run/docker.sock` (optional) | Powers the Containers tab; equivalent to host root, so only mount on a trusted/authenticated deployment. Removing it makes `/api/containers` return 503 |

There is no separate database container; SQLite lives in the data volume. Community modules are opt-in and are not part of the base compose file.

## Licensing note

AgeniusDesk CE is MIT licensed with no license-key or tier system; there is no `backend/license.py` in this edition. Module gating is purely by `min_app_version` (see [Module System](modules.md)), not by a paid tier.

## See also

- [Module System](modules.md)
- [Data Model](data-model.md)
- [Authentication & RBAC](auth.md)
- [Frontend](frontend.md)
- [API Reference](api.md)
- [Security](security.md)
- User guide: [../guide/](../guide/)
