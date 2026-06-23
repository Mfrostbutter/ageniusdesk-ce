# Changelog

All notable changes to AgeniusDesk Community Edition are documented here.

## [0.1.0] - 2026-06-23

### Initial Release

The first open-source, MIT-licensed release of AgeniusDesk Community Edition.

#### Features

**Multi-Instance Management**
- Add unlimited n8n instances by URL and API key
- Switch between instances instantly
- Encrypted credential storage with Fernet (AES-128-CBC + HMAC-SHA256)
- Secret references via `$VAR_NAME` syntax

**Error Visibility**
- Real-time error feed with WebSocket streaming
- Errors grouped by workflow, node, and error type
- Occurrence counts and last-seen timestamps
- Global error handler workflow for seamless integration

**Workflow Management**
- List, search, activate/deactivate workflows
- Trigger workflows on demand
- View execution history per workflow
- Import, export, and backup workflows

**Code Lab**
- Monaco editor for n8n Code-node JavaScript/TypeScript/Python
- AI assistance for code generation and explanation
- Syntax highlighting and n8n node introspection
- One-click "Send to n8n" deployment

**AI Assistant**
- Support for OpenRouter, OpenAI, Anthropic, and local Ollama
- Function calling to query workflows, manage executions, and analyze errors
- MCP server integration for extending with external tools
- Optional RAG via Qdrant for knowledge-based context
- Custom system instructions and knowledge file uploads

**Knowledge Management**
- External knowledge source registration
- Markdown notes vault compatible with Obsidian
- Full-text search with BM25 ranking
- Tag-based organization, backlinks, and wikilinks
- Folder tree structure for note organization

**Container Management**
- List, inspect, and manage Docker containers
- One-click container deployment with built-in templates
- Community template library (drop JSON into `data/templates/`)
- Container lifecycle actions (logs, recreate, destroy)
- Multi-container bundle support with shared networking

**Secrets Store**
- Fernet-encrypted credential storage at rest
- Environment variable resolution with encrypted fallback
- Reference secrets as `$VAR_NAME` anywhere in the app

**Notifications**
- Inbound webhook for dashboard messages (`/api/messages/webhook`)
- Messages persisted and broadcast to all open tabs as toasts
- Generic `SLACK_WEBHOOK_URL` / `DISCORD_WEBHOOK_URL` env vars for operator-supplied sinks (no keys hardcoded)

**Insights & Analytics**
- Execution success rates and error trends
- Per-instance health status tracking
- Workflow busiest / slowest statistics

**Themes & Customization**
- 4 built-in themes (Dark, Light, Cyberpunk, Matrix)
- Custom theme support via JSON
- Music player integration (Spotify, YouTube, SoundCloud, Apple Music, Tidal)

**Deployment & Operations**
- Single-command Docker Compose setup
- Bare-metal Python 3.10+ installation supported
- TLS verification gating via `AGD_TLS_VERIFY`
- Optional authentication (`AGD_REQUIRE_AUTH=true`)
- Comprehensive configuration via environment variables

#### Architecture

- Python 3.10+ FastAPI backend with async I/O
- Vanilla JavaScript frontend with zero build step
- SQLite default storage (PostgreSQL optional)
- Auto-discovered module system for extensibility
- WebSocket server for real-time updates
- Public API at `/api/v1` with X-API-Key authentication

#### Documentation

- README with quickstart guide
- Full API reference
- Deployment runbook for self-hosting
- Configuration reference
- Contributing guidelines

#### Removed (from the internal fork this release is derived from)

Stripped to keep the open-source tree generic and free of operator-specific wiring:

- Outbound notification-router module that was hard-wired to private Slack/Discord
  channels and event classes (morning brief, analytics digest, P0/backup alerts).
  Outbound notifications are now just the generic `SLACK_WEBHOOK_URL` /
  `DISCORD_WEBHOOK_URL` env vars; the inbound message bus is unchanged.
- `metric_snapshots` and `agent_runs` database tables (internal analytics cache and
  agent-run telemetry); neither had any reader or writer in the Community Edition.
- Operator-specific references in module metadata and code comments (private vector
  collection names, internal workstation/host names).

