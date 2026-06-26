# Changelog

All notable changes to AgeniusDesk Community Edition are documented here.

## [Unreleased]

### Added
- New built-in **n8n** dark theme, styled after the n8n product (solid neutral-gray surfaces, orange accent, teal-green success). Brings the built-in theme count to three (Dark, Light, n8n).
- Instances, Models, and MCP are now first-class sidebar views instead of deep-links into Settings. Clicking them shows a focused, single-purpose page (no Settings tab strip); the same panels still live under the Settings gear as tabs. This also fixes the wrong (Settings) coachmark firing on those pages.
- Page coachmarks now cover every primary view: a single orienting bubble on Overview, Workflows, Executions/Errors, and Containers, plus dedicated tours for Instances, Models, and MCP. The Code Lab tour now calls out the Prompt Builder.
- Security hardening: internal `/api/*` routes now have a central auth gate, edge-auth headers are trusted only with `AGD_TRUST_EDGE_AUTH=true`, legacy ingest webhooks can be protected with `AGD_WEBHOOK_TOKEN`, and external dashboard MCP clients can use `DASHBOARD_MCP_TOKEN`.
- Test suite (`tests/`): first automated regression coverage, pinning the security-hardening behaviors that had no other safety net. Covers the fail-closed internal-API middleware (public allowlist passes, private routes 401 without identity and pass with an admin token), edge-auth trusted only when `AGD_TRUST_EDGE_AUTH=true`, legacy webhook token enforcement (bearer + `X-AGD-Webhook-Token`), theme- and JS-path traversal guards, and the no-account-enumeration property of password recovery. Run with `uv run pytest`.

### Fixed
- Models: each area (Code Lab / Error Triage / General Assistant) now has an **API key** dropdown listing your stored secrets, so a key saved under any name (e.g. `$OPEN_ROUTER_API_KEY`) can be selected directly. Previously the area only resolved a single hard-coded convention name (`$OPEN_ROUTER_KEY`) with no way to point at a differently-named secret. Leaving it on "Use provider default key" keeps the convention behavior. Key resolution at chat time honors the per-area selection.
- Models: selecting an API key now **tests the connection and reloads the live model list using that key**, so you see every model the key can reach instead of the short hardcoded fallback (e.g. OpenRouter jumps from ~11 fallback entries to the full live catalog). The `/models` and `/test-creds` endpoints accept a secret ref and resolve it server-side (the plaintext key never returns to the browser); the model cache is keyed per-key so areas with different keys don't evict each other.
- Models: each of the three areas (Code Lab, Error Triage, General Assistant) has its own **Test & load models** button, so every area/provider can be validated and its live model list pulled independently. The OpenRouter connection test now hits the key-validation endpoint (its model list is public and returned 200 even for bad keys), so an invalid key is correctly reported.
- Models: each area now has its own **Save** button that persists just that area (partial save), replacing the single floating "Save all areas" bar.
- Models: testing an area with "Use provider default key" when no key is configured now returns a clear "No API key set" message instead of crashing with `Illegal header value b'Bearer '` (an empty key was being sent as an empty `Bearer` header). The default-key test also resolves the per-provider convention secret.
- The post-wizard "Connect your n8n" guide no longer collides with a dashboard coachmark: the Overview/dashboard bubble was removed (the dashboard is self-explanatory and is where the get-started card and connect guide already live).
- Dashboard "command center" tip anchors on the always-present "+ Widget" control instead of the widget grid, which is empty (zero-height) until widgets load async and would otherwise mark the tip seen before it ever showed.
- CSRF protection regressed any action using a raw `fetch()` that bypassed the API helper (workflow delete / delete-archived, container lifecycle, music player) — they returned "failed" because the CSRF token wasn't attached. A global `fetch` shim now adds the token to every same-origin mutation, covering current and future callers.
- Error-handler install is idempotent: it reuses an existing "Global Error Handler" workflow instead of importing a duplicate, and the post-connect prompt detects an already-installed handler and just confirms it.
- Static/theme path handling now resolves paths under the intended frontend or `data/themes` directory before reading or writing, closing traversal edge cases in custom asset handlers.

**Viewport, onboarding polish, and password policy**
- Site-wide horizontal-overflow guard: nothing produces a horizontal scrollbar. Fixed the coachmark spotlight overshooting the viewport (it now cancels the app's body `zoom` so it maps 1:1 to the screen).
- Page coachmarks lead with what each area is for. The discovery-heavy workspaces (Code Lab, the Harness, MCP, Models, Secrets) get a short multi-step walkthrough; the self-explanatory list views (Overview, Workflows, Errors, Containers) get a single orienting bubble.
- Stronger password policy: 12+ characters with an uppercase, lowercase, number, and symbol (each class configurable via `AGD_PASSWORD_REQUIRE_*`). Setup, reset, and change-password show a live requirements checklist; the same rules are enforced server-side.
- Setup wizard: the "stand up my stack" path no longer skips straight to the end. After the stack deploys it continues through Secrets and AI Assistant, then the dashboard shows a guided "connect your n8n" prompt (open n8n, create the account, mint an API key, register it). The "Sync to n8n" step was removed entirely, and the step indicator now shows only the steps that apply to your chosen path.
- Docker networking: when the dashboard runs in a container, an n8n URL of `localhost` is transparently reached via `host.docker.internal` (stored as the backend URL while the browser link keeps `localhost`), so connecting a host-published n8n no longer fails with "connection refused." The connect form also pre-fills the URL with the host you reached the dashboard by, so accessing AgeniusDesk via a LAN IP auto-fills that IP.

**Onboarding and page tips**
- Setup Journey "Get started" card on the Dashboard. Milestone completion is derived live from app state (n8n connected, secrets added, AI configured, 2FA on, harness visited), so it stays honest and resumable instead of tracking a stored step. Auto-hides once core setup is done; dismissible and reopenable from Settings.
- Page coachmarks: a dependency-free spotlight + bubble walkthrough that runs the first time you open a view, pointing out the key controls (Dashboard, Workflows, Errors, Code Lab, Knowledge, Secrets, Settings, Containers). Tolerates missing anchors, honors `prefers-reduced-motion`, and is keyboard and screen-reader friendly.
- Settings > Help & Tips: toggle page tips, replay the current page's tour, reset all tips, and reopen the setup checklist or wizard. Persistence is per-browser (localStorage).

**Authentication, accounts, and 2FA**
- Built-in login with local accounts. First browser visit forces creation of an owner account (admin), then requires sign-in. The owner account is keyed by **email** (the login identity, also used for recovery); the setup screen spells out the password requirement. Edge identity (Cloudflare Access) can satisfy the gate only when `AGD_TRUST_EDGE_AUTH=true`, and the `AGD_ADMIN_TOKEN` bearer can satisfy the gate when configured.
- Forgot-password flow: a "Forgot password?" link issues a single-use, time-limited reset link to the account email (no account-enumeration in the response). Completing a reset invalidates all other sessions. Delivery uses SMTP (`AGD_SMTP_*`); when SMTP is unconfigured the link is logged so self-hosted installs without a mail server can still recover access.
- Auth bootstrap endpoints (`/setup`, `/login`, `/forgot`, `/reset`) are exempt from the CSRF gate, fixing a first-run failure where a stale `agd_session` cookie (left after a data-volume wipe, or shared across localhost ports) blocked account creation with "CSRF check failed."
- Optional TOTP two-factor (any authenticator app), with one-time recovery codes. QR rendered client-side from a vendored, dependency-free generator; the setup key is also shown for manual entry.
- Server-side sessions stored as a SHA-256 of the token (a DB leak cannot be replayed), `HttpOnly` + `SameSite=Strict` cookie, sliding expiry with an absolute cap, server-side revocation, and a session list in Settings > Account.
- Double-submit CSRF protection on cookie-authenticated mutations.
- Coarse role-based access control (`viewer < operator < admin`): admin/secrets require `admin`; the n8n and container control surfaces require `operator`; read surfaces require any signed-in user. Machine webhooks (errors, messages) stay open for n8n ingestion.
- Password hashing raised to PBKDF2-HMAC-SHA256 at 600k iterations with login-time rehash of legacy hashes; minimum password length raised to 12.
- New settings: `AGD_DISABLE_LOGIN`, `AGD_SESSION_TTL_DAYS`, `AGD_SESSION_ABSOLUTE_DAYS`, `AGD_LOGIN_MAX_ATTEMPTS`, `AGD_LOGIN_LOCKOUT_MINUTES`, `AGD_PASSWORD_MIN_LENGTH`, `AGD_PASSWORD_REQUIRE_*`, `AGD_PASSWORD_RESET_TTL_MINUTES`, `AGD_TRUST_EDGE_AUTH`, `AGD_TRUST_FORWARDED_FOR`, `AGD_WEBHOOK_TOKEN`.
- `harden_file_permissions()` now also `chmod 600`s `users.json` and `dashboard.db`.

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
- 2 built-in themes (Dark, Light) plus custom theme support
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
