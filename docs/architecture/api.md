# API Reference

AgeniusDesk CE exposes three distinct HTTP surfaces: the internal `/api/*` routes the browser UI uses (session-cookie authenticated, CSRF-protected), the versioned public `/api/v1/*` REST surface for external integrations (X-API-Key authenticated, CSRF-exempt), and the Dashboard-as-MCP server at `/api/mcp-dashboard` (FastMCP streamable HTTP, bearer-token authenticated, or reachable through the browser session gate). This page documents the latter two. Everything is mounted in `backend/main.py`.

See also: [Architecture Overview](overview.md), [Authentication & RBAC](auth.md), [Security Posture](security.md), [Data Model](data-model.md), [Module System](modules.md), [Configuration](../CONFIG.md).

## Three surfaces at a glance

| Surface | Path | Auth | CSRF | Mounted as |
|---|---|---|---|---|
| Internal API | `/api/*` | session cookie or edge identity (the internal gate) | required on mutations (`X-AGD-CSRF`) | routers via `register_modules(app)` |
| Public API v1 | `/api/v1/*` | `X-API-Key` header | exempt | separate FastAPI sub-app |
| Dashboard MCP | `/api/mcp-dashboard` | `Authorization: Bearer <DASHBOARD_MCP_TOKEN>`, or browser session via the internal gate | n/a (bearer/JSON-RPC) | FastMCP streamable-HTTP app |

The internal-API gate (`require_internal_api_auth` middleware) makes `/api/*` private by default. It lets through a small allowlist of public endpoints (`/api/status`, `/api/health/docker-env`, the `/api/auth/*` bootstrap endpoints), the `/api/v1/` prefix (handled by its own key auth), the legacy webhook endpoints (their own token, below), the self-authenticating music trigger, and the dashboard MCP prefix when its token matches. Everything else requires a resolved identity. Details in [Authentication & RBAC](auth.md) and [Security Posture](security.md).

## Public API v1 (`/api/v1`)

Mounted in `main.py` as a separate FastAPI sub-app so its OpenAPI docs are isolated at `/api/v1/docs` (`openapi.json` at `/api/v1/openapi.json`). Starlette strips the `/api/v1` mount prefix before routing, so handlers in `backend/modules/public_api/router.py` use bare paths.

### Authentication

Send your key in the `X-API-Key` header. Keys are validated in `backend/modules/public_api/auth.py`: the supplied key is SHA-256 hashed and looked up against `data/api_keys.json`. Raw keys are never stored, only `sha256(raw_key)`, so a leaked `api_keys.json` cannot be replayed.

Keys carry one of two scopes, with `trigger` a superset of `read`:

| Scope | Satisfies | Use |
|---|---|---|
| `read` | read endpoints | GET endpoints and the webhooks |
| `trigger` | read + trigger | also the workflow-trigger POST |

`require_scope("read")` accepts both scopes; `require_scope("trigger")` accepts only `trigger`. The webhook endpoints use bare `verify_api_key` (any valid key) so an n8n global error handler can post with a read-scoped key.

Create and revoke keys via the internal admin routes (`backend/modules/admin/router.py`): `POST /api/admin/api-keys` (returns the raw key exactly once), `GET /api/admin/api-keys` (metadata only), `DELETE /api/admin/api-keys/{key_id}`.

### Endpoints

All paths below are relative to the `/api/v1` mount.

| Method | Path | Scope | Description |
|---|---|---|---|
| GET | `/status` | read | Configured flag, version, active instance, health endpoints |
| GET | `/n8n/instances` | read | List configured n8n instances (no API keys exposed) |
| GET | `/n8n/workflows` | read | List workflows on the active instance. Query: `active_only`, `name_contains`, `limit` (def 50), `cursor` |
| GET | `/n8n/workflows/{workflow_id}` | read | Single workflow; 404 if missing |
| GET | `/n8n/executions` | read | Recent executions. Query: `workflow_id`, `status`, `limit` (def 20), `cursor` |
| GET | `/n8n/executions/{execution_id}` | read | Single execution; 404 if missing |
| GET | `/errors` | read | Recent stored errors. Query: `limit` (def 50), `offset`, `workflow_id`, `range`. Returns `{errors, count_24h}` |
| POST | `/n8n/workflows/{workflow_id}/trigger` | trigger | Trigger a workflow. Body `{payload?: object}` |
| POST | `/errors/webhook` | any valid key | Receive an error from an n8n global error handler |
| POST | `/messages/webhook` | any valid key | Receive a dashboard message/notification |
| GET | `/ha/summary` | read | Aggregated fleet status for a Home Assistant coordinator |

`/status`, list, and trigger endpoints return `503` ("No n8n instances configured") when no instance is set up.

`/ha/summary` is a thin auth wrapper over `summary.build_ha_summary()` (`backend/modules/public_api/summary.py`); the same aggregation is reused by in-process callers. It returns `workflow_count`, `error_count_24h`, `last_execution_at`, `health_status`, `version`, and the active `instance`. Note `health_status` reports `degraded` (not `healthy`) when the upstream n8n read fails, so an outage is not masked. The `workflow_count` is a heuristic derived from a `limit=1` probe unless the upstream response carries an explicit `count` (documented in the source).

### curl examples

```bash
# List workflows on the active instance
curl -H "X-API-Key: agd_xxxxxxxx" \
  "https://app.example.com/api/v1/n8n/workflows?limit=10"

# Trigger a workflow (needs a trigger-scoped key)
curl -X POST \
  -H "X-API-Key: agd_xxxxxxxx" \
  -H "Content-Type: application/json" \
  -d '{"payload": {"hello": "world"}}' \
  "https://app.example.com/api/v1/n8n/workflows/abc123/trigger"

# Post an error from an n8n error handler (any valid key)
curl -X POST \
  -H "X-API-Key: agd_xxxxxxxx" \
  -H "Content-Type: application/json" \
  -d '{"workflow_id":"abc123","workflow_name":"My WF","error_message":"boom","error_type":"NodeError"}' \
  "https://app.example.com/api/v1/errors/webhook"

# HA coordinator poll
curl -H "X-API-Key: agd_xxxxxxxx" \
  "https://app.example.com/api/v1/ha/summary"
```

### How v1 differs from the internal `/api` routes

- **CSRF-exempt.** The `csrf_protect` middleware in `main.py` explicitly skips the `/api/v1/` prefix; CSRF only applies to cookie-authenticated browser mutations, and v1 is key-authed.
- **Key-authed, not session-authed.** The internal-API gate allowlists the `/api/v1/` prefix so v1 enforces its own `X-API-Key` scheme rather than the session/edge gate.
- **No instance-switch semantics in the URL.** v1 reads/writes against the currently active instance (the same one the UI targets).

## Dashboard-as-MCP server (`/api/mcp-dashboard`)

`backend/modules/dashboard_mcp/server.py` builds a `FastMCP` streamable-HTTP server and mounts it at `/api/mcp-dashboard` (`mount_on(app)` from `main.py`, after `register_modules`). The JSON-RPC endpoint is served at the root of that mount (`streamable_http_path="/"`), so clients point directly at `/api/mcp-dashboard`. It gives a `claude` instance running in a terminal sidecar first-class, read-mostly access to the same data the UI shows.

Because mounted sub-app lifespans are not run by FastAPI, `main.py` drives the FastMCP session manager from the app lifespan (`_mcp_sm.run()`).

### Authentication

Two paths satisfy the internal gate for this prefix:

- **Bearer token.** Set `DASHBOARD_MCP_TOKEN` and send `Authorization: Bearer <token>`. The gate's `_dashboard_mcp_token_ok()` does a constant-time compare. Set this before exposing the endpoint to any non-browser client.
- **Browser session.** A logged-in browser reaches it through the dashboard's normal internal API gate (the session cookie), so the in-app `claude` sidecar works without a separate token.

If `DASHBOARD_MCP_TOKEN` is unset, the bearer path is not satisfied and access falls back to the session gate. A convenience health/auth probe lives at `GET /api/mcp-dashboard/_meta/ping`, returning `{"ok": true, "auth": "open"|"ok"}` depending on whether a token is configured and supplied.

FastMCP's DNS-rebinding protection rejects `Host` headers not on its allowlist. The default allowlist is `dashboard:3000, localhost:3000, 127.0.0.1:3000, localhost, 127.0.0.1`; override with `DASHBOARD_MCP_ALLOWED_HOSTS` (comma-separated) for custom deployments.

### Tools exposed

All tools are read or vault-scoped; none mutate n8n. Secrets return names/types only, never values.

| Tool | Returns |
|---|---|
| `list_workflows(active_only, name_contains, limit)` | n8n workflows on the active instance |
| `get_workflow(workflow_id)` | full workflow definition (nodes + connections) |
| `list_executions(workflow_id, status, limit)` | recent executions |
| `list_errors(limit, hours)` | recent webhook-reported errors (time-filtered) |
| `list_n8n_instances()` | configured instances (api_key omitted; `key_hint` only) |
| `list_secrets_metadata()` | secret names + kind/type, never values |
| `list_mcp_servers()` | configured MCP servers (`has_token` boolean, no token value) |
| `list_messages(limit)` | recent webhook-posted dashboard messages |
| `get_status()` | dashboard's own status |
| `search_notes(query, tag, limit)` | full-text/tag search over the notes vault |
| `read_note(path)` | one note's raw markdown by vault-relative path |
| `write_note(path, content)` | create/overwrite a note |
| `append_note(path, content)` | append to (or create) a note |
| `list_backlinks(path)` | notes that wikilink to the given note |
| `list_note_tags()` | unique tags with counts |
| `list_knowledge_sources()` | registered external knowledge sources (with descriptions) |
| `search_knowledge(query, sources, limit)` | semantic search across selected/all enabled sources |

The tools are thin wrappers over in-process helpers (`n8n_proxy.client`, `get_db()`, `config.load_secrets`, the notes and knowledge modules), not HTTP self-calls, so they run in the same event loop without loop overhead.

### curl examples

```bash
# Health/auth probe (no token configured -> auth: open)
curl https://app.example.com/api/mcp-dashboard/_meta/ping

# Probe with a configured token
curl -H "Authorization: Bearer $DASHBOARD_MCP_TOKEN" \
  https://app.example.com/api/mcp-dashboard/_meta/ping

# MCP initialize handshake (JSON-RPC over streamable HTTP)
curl -X POST https://app.example.com/api/mcp-dashboard \
  -H "Authorization: Bearer $DASHBOARD_MCP_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'
```

In practice you point an MCP client (for example `claude` configured with an MCP server URL) at `/api/mcp-dashboard` rather than driving the JSON-RPC frames by hand. The server registers itself in AgeniusDesk's own MCP config on first run so the terminal sync picks it up automatically.
