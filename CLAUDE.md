# AgeniusDesk Community Edition - Claude Code Context

Last updated: 2026-06-23

An open-source command center for managing multiple n8n automation instances. MIT licensed, built for operators who need a unified control plane across their n8n fleet.

## Tech Stack

- **Backend:** Python 3.10+, FastAPI (uvicorn), httpx, aiosqlite
- **Frontend:** Vanilla JavaScript ES modules, zero build step, Monaco editor loaded from CDN
- **Storage:** SQLite (data/dashboard.db) plus Fernet-encrypted secret store (data/secrets.json)
- **Container:** Docker Compose, single service, bind-mounts Docker socket for container management
- **Conventions:** Type hints, ruff (line-length 120, rules E/F/I/W), conventional commits

## Architecture Highlights

**Backend Structure:**
- `backend/main.py` - FastAPI entry point, CORS, WebSocket at `/ws`, static file serving
- `backend/modules/` - auto-discovered packages, each exposes a `router` and `manifest.json`
  - `n8n_proxy/` - multi-instance proxy for workflows, executions, credentials
  - `n8n_promote/` - workflow promotion across instances (preflight, credential mapping + auto-provision from Secrets, activation guarding)
  - `errors/` - error collection, grouping by workflow, node, error type
  - `assistant/` - AI chat with LLM providers (OpenRouter, OpenAI, Anthropic, Ollama, plus Perplexity, Groq, DeepSeek, Mistral, xAI, Together, and a Custom OpenAI-compatible endpoint), MCP client, RAG
  - `agent_fleet/` - managed LangGraph + PydanticAI agent fleet (catalog, live graph, run waterfall, human-in-the-loop); opt-in `langgraph` extra, n8n-only by default
  - `docker_mgr/` - container lifecycle management, templates, deployment
  - `knowledge/` - external knowledge source management and RAG backends
  - `notes/` - markdown notes vault with search, tagging, backlinks
  - `insights/` - execution analytics, success rates, error trends
  - `observability/` - OpenTelemetry OTLP receiver, trace waterfall, metrics strip, LLM cost enrichment
  - `messages/` - inbound message-bus webhook persisted and broadcast as toasts (optional Slack, Discord env sinks)
  - `player/` - music player integration
  - `auth/` - local accounts, sessions, optional TOTP 2FA, CSRF, role-based access
  - `admin/` - user management, encrypted secret store, settings
  - `public_api/` - versioned /api/v1 endpoints with X-API-Key authentication
  - `dashboard_mcp/` - built-in MCP server (FastMCP, streamable HTTP)
  - `themes/`, `webhooks/`, `health/`, `modules/`, `n8n_credentials/` - supporting modules

**Frontend:**
- `frontend/index.html` - single-page app shell with sidebar navigation
- `frontend/js/views/*.js` - each view is an ES module exporting `async render(container)`
- `frontend/js/app.js` - router, WebSocket manager, view lifecycle
- `frontend/css/base.css` - theme-aware CSS custom properties

**Data Model (SQLite):**
- `errors` - error records with instance_id, workflow, node, error_type, count, last_seen
- `messages` - notification toasts (title, body, level, source, occurred_at)
- `health_checks` - per-instance health state
- `notes` - markdown documents with metadata
- Other tables - per-module state (insights aggregations, etc)

**Secret Resolution:**
Environment variable first, then encrypted `data/secrets.json`. Secret refs like `$MY_KEY` resolve left-to-right; env var wins.

**TLS:**
All outbound httpx calls respect `AGD_TLS_VERIFY` (default true). For self-signed n8n on private LAN, set `AGD_TLS_VERIFY=false`.

## Module System

Each module at `backend/modules/{id}/` contains:
- `__init__.py` - exposes `router` (FastAPI APIRouter)
- `manifest.json` - module metadata
- Supporting files (client.py, router.py, service.py, etc)

The loader auto-discovers and mounts all modules at `/api/{id}/...`.

## Conventions

- **Python:** Type hints on public functions, ruff at line-length 120, conventional commits
- **Frontend:** Vanilla JS, no build step, ES modules only, CSS via theme custom properties
- **Secrets:** Never hardcode; all sensitive values go to .env or encrypted store
- **MIT License:** All new source files are MIT
