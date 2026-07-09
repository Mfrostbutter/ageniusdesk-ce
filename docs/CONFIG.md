# Configuration Reference

AgeniusDesk CE configuration is managed via environment variables. Create a `.env` file in the repo root (copy from `.env.example` as a starting point) and customize the values below.

## Core Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `3000` | Port the dashboard listens on |
| `SECRET_KEY` | Auto-generated | Master key for encrypting secrets. If not set, generated and persisted to `data/.secret_key` (mode 600) on first run. Losing this file makes all encrypted values unrecoverable. Back it up. |
| `AGD_REQUIRE_AUTH` | `false` | Extra hard gate for token/edge-auth deployments. Local account login is enforced by default unless `AGD_DISABLE_LOGIN=true`; set this to keep auth required even when browser login is disabled. |
| `AGD_TLS_VERIFY` | `true` | Verify TLS certificates on outbound HTTP calls to n8n instances. Set to `false` only for self-signed certificates on private LANs. |

## Database

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | SQLite at `data/dashboard.db` | Optional PostgreSQL connection string (e.g., `postgresql://user:pass@localhost/ageniusdesk`). Leave blank to use SQLite. |

## AI Assistant Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `ANTHROPIC_KEY` | (none) | Anthropic API key for Claude models. Can also set as `ANTHROPIC_API_KEY`. |
| `OPEN_AI_KEY` | (none) | OpenAI API key for GPT models. Can also set as `OPENAI_API_KEY`. |
| `OPEN_ROUTER_KEY` | (none) | OpenRouter API key. One key reaches hundreds of models across providers; a good default. |
| `OLLAMA_URL` | `http://localhost:11434` | Local Ollama endpoint for self-hosted models. Inside Docker, use `http://host.docker.internal:11434` or your LAN IP. |
| `PERPLEXITY_KEY` | (none) | Perplexity API key (Sonar models). |
| `GROQ_KEY` | (none) | Groq API key (fast Llama / Qwen / DeepSeek-distill inference). |
| `DEEPSEEK_KEY` | (none) | DeepSeek API key (deepseek-chat / deepseek-reasoner). |
| `MISTRAL_KEY` | (none) | Mistral API key (Mistral / Codestral models). |
| `XAI_KEY` | (none) | xAI API key (Grok models). |
| `TOGETHER_KEY` | (none) | Together AI API key (open models). |
| `CUSTOM_LLM_KEY` | (none) | API key for the **Custom** provider: any OpenAI-compatible endpoint (Azure OpenAI, LiteLLM, vLLM, LocalAI, Fireworks, ...). Set its base URL in Models > "Custom OpenAI-compatible endpoint" (stored as `assistant.custom_base_url`). |

All of the above except Ollama are OpenAI-compatible and route through the same chat path. Each assistant area (Code Lab / Error Triage / General Assistant) selects its own provider and model independently.

## Agent Fleet

| Variable | Default | Description |
|----------|---------|-------------|
| `AGD_AGENTS_ENABLED` | (auto) | Controls the Agent Fleet view and Code Lab's Agent Builder mode. Unset = **auto**: shown only when the optional agent extra (`AGD_EXTRAS="...,langgraph"`) is installed, so a default install reads as a pure n8n control plane. Set `false` to hide the agent surface even with the extra present (n8n-only experience); set `true` to force it on. Hiding is UI-only; it does not change which dependencies are installed. |

## Knowledge & RAG

| Variable | Default | Description |
|----------|---------|-------------|
| `QDRANT_URL` | (none) | Qdrant vector database endpoint (e.g., `http://localhost:6333`). Optional; enables RAG for knowledge sources. |
| `QDRANT_API_KEY` | (none) | API key for Qdrant (if required). |
| `EMBEDDING_PROVIDER` | `openai` | Embedding model provider: `openai`, `voyage`, `ollama`. |
| `EMBEDDING_MODEL` | `text-embedding-3-small` | Embedding model name (e.g., `text-embedding-3-large` for OpenAI). |
| `SEARCH_PROVIDER` | `none` | Web search provider for assistant: `tavily`, `serper`, `none`. |
| `SEARCH_API_KEY` | (none) | API key for web search provider. |

## Observability & Tracing

Tracing and per-run cost are self-contained: no external account or SaaS is required. LangSmith is an optional integration for teams already building on that platform.

| Variable | Default | Description |
|----------|---------|-------------|
| `AGD_OTEL_ENABLED` | `false` | Enable the embedded OpenTelemetry OTLP/HTTP receiver. n8n and the Agent Fleet export workflow, node, and agent spans straight to AgeniusDesk; the Observability view renders them as a trace waterfall. No external service. |
| `AGD_OTEL_TOKEN` | (none) | Bearer token gating the OTLP ingest endpoint. Unset = open (trusted-LAN only). Set this before exposing the port publicly. |
| `AGD_OTEL_RETENTION_HOURS` | `72` | Age-based span pruning; spans older than this are dropped. |
| `AGD_OTEL_MAX_SPANS` | `500000` | Hard cap on stored spans; the oldest are pruned first past this limit. |
| `AGD_HEALTH_MIN_SAMPLES` | `20` | Silent-failure low-output classifier: runs of history a node needs before it can be judged. Below this the node is cold-start and never flags. |
| `AGD_HEALTH_STEADY_ZERO_RATE` | `0.05` | Zero-rate at or under which a node counts as a reliable producer; a zero/low output on such a node is a silent failure. |
| `AGD_HEALTH_DORMANT_ZERO_RATE` | `0.95` | Zero-rate at or over which a node is treated as normally-empty (a poller/filter); its zeros never flag. |
| `AGD_HEALTH_DROP_FACTOR` | `0.1` | An output below `median * this` for a reliable producer is a magnitude-drop anomaly (e.g. 200 → 3). |
| `AGD_HEALTH_WINDOW` | `200` | Rolling history size (runs) per node for the classifier. |
| `AGD_PRICEBOOK_REFRESH_HOURS` | `24` | How often to refresh the LLM price book from OpenRouter's public models API. Cached to `data/price_book.json` with a last-good fallback. Per-run cost is computed locally from token counts against this book. |
| `LANGSMITH_TRACING` | `false` | **Optional.** Set to `true` (with `LANGSMITH_API_KEY`) to also send Agent Fleet runs to LangSmith. For teams already on LangSmith; not required for tracing or cost. When on, it overrides the local price-book cost estimate with LangSmith's exact figures and adds a per-call breakdown plus an external trace link. Self-disables if the key is missing. |
| `LANGSMITH_API_KEY` | (none) | LangSmith API key. Only used when `LANGSMITH_TRACING=true`. A LangSmith account is needed only for this optional integration. |

## Notifications

| Variable | Default | Description |
|----------|---------|-------------|
| `SLACK_WEBHOOK_URL` | (none) | Slack incoming webhook for notifications. Set on Admin > Notifications. |
| `DISCORD_WEBHOOK_URL` | (none) | Discord webhook for notifications. Set on Admin > Notifications. |

## Docker / Containers Tab

| Variable | Default | Description |
|----------|---------|-------------|
| `AGD_HOST_ALIASES` | (none) | Comma-separated list of hostnames or LAN IPs this host is known by (e.g., `192.0.2.10,myhost.local`). Required when the dashboard runs in Docker and n8n instance URLs use the host's LAN IP or hostname; the container's routing namespace doesn't see those addresses, so without this the container update/deploy flow cannot reach the host's Docker daemon. |
| `AGD_PUBLIC_HOST` | (auto-detect) | Override the public hostname for deployed container URLs (e.g., `ageniusdesk.example.com`). Defaults to the request Host header or `localhost`. Used in deploy-done banners and instance-registration pre-fills. |

> **Security:** the Containers tab requires mounting the Docker socket (see `docker-compose.yml`). That grants the dashboard root-equivalent control of the host. Only run it mounted when the dashboard is not exposed unauthenticated: keep it on a trusted LAN, front it with an auth proxy, or set `AGD_REQUIRE_AUTH=true`. Remove the socket mount to disable the Containers tab.

## Community Module Isolation

How a community module's BACKEND runs. Set in **Settings > Modules**, or via the
env var below (env overrides the saved setting). Changing it restarts the app.
Privileged actions in the isolated tiers go through a loopback capability bridge
(vault access scoped to declared paths; a tool-free `assistant.complete` that
keeps the provider key host-side). See `docs/architecture/modules.md` for the
full comparison.

| Variable | Default | Description |
|----------|---------|-------------|
| `AGD_MODULE_ISOLATION` | `in_process` | `in_process` (no isolation; modules run in the app process), `subprocess` (sandboxed child process: blocked host imports, scrubbed env, reverse proxy), or `container` (own hardened Docker container per module: read-only rootfs, dropped capabilities, no socket, resource limits, isolated network). `container` requires the Docker socket mounted (see Containers tab). |

> **Trade-off:** strength is `in_process` < `subprocess` < `container`, and so is
> overhead. `in_process` has no runtime boundary (the install scan/consent is a
> heuristic, not containment). `subprocess` raises the bar but shares the host OS
> user. `container` is the real boundary but needs Docker. v1 container caveats:
> the worker runs as root inside the cap-dropped container, and a module that
> declares network can reach any host (per-host enforcement is on the roadmap).

## Observability (OpenTelemetry)

The dashboard can receive per-execution, per-node spans from n8n's native OTel
exporter and render them as a trace waterfall in the Observability view. Off by
default. The ingest endpoint (`POST /api/otel/v1/traces`) is a machine-ingest
surface, exempt from the session gate and protected by `AGD_OTEL_TOKEN`; set the
token before exposing the port beyond a trusted LAN.

| Variable | Default | Description |
|----------|---------|-------------|
| `AGD_OTEL_ENABLED` | `false` | Enable the embedded OTLP/HTTP receiver. When off, the ingest endpoint returns `404`. |
| `AGD_OTEL_TOKEN` | (none) | Bearer (or `X-AGD-Otel-Token`) token n8n must send. Unset = open (trusted-LAN only); compared in constant time. On n8n: `N8N_OTEL_EXPORTER_OTLP_ENDPOINT=http://<host>:<PORT>/api/otel` and `N8N_OTEL_EXPORTER_OTLP_HEADERS=authorization=Bearer <token>`. |
| `AGD_OTEL_RETENTION_HOURS` | `72` | Spans older than this are pruned on ingest. |
| `AGD_OTEL_MAX_SPANS` | `500000` | Hard cap on stored spans; oldest dropped past it. |

The query endpoints under `/api/otel/*` (trace list, waterfall, metrics, cost)
are ordinary session-authed routes; the price-book refresh (`POST
/api/otel/pricing/refresh`) requires `operator`.

## Security

| Variable | Default | Description |
|----------|---------|-------------|
| `AGD_REQUIRE_AUTH` | `false` | Keep internal API auth required even if `AGD_DISABLE_LOGIN=true`; useful for token/edge-auth automation deployments. |
| `AGD_ADMIN_TOKEN` | (none) | Bearer token accepted on internal API routes when set. Compared in constant time. |
| `AGD_TRUST_EDGE_AUTH` | `false` | Trust Cloudflare Access / reverse-proxy identity headers (`Cf-Access-Authenticated-User-Email`, `X-Forwarded-User`). Enable only when the dashboard is reachable exclusively through that trusted proxy. |
| `AGD_TRUST_FORWARDED_FOR` | `false` | Use `X-Forwarded-For` for login throttling. Enable only behind a trusted proxy. |
| `AGD_WEBHOOK_TOKEN` | (none) | Optional bearer or `X-AGD-Webhook-Token` token for legacy `/api/errors/webhook` and `/api/messages/webhook` ingestion. When unset, those legacy endpoints remain open for backward compatibility; new integrations should use the X-API-Key protected `/api/v1/...` webhooks. |
| `DASHBOARD_MCP_TOKEN` | (none) | Optional bearer token for external clients calling `/api/mcp-dashboard`. Without it, browser sessions can still use the endpoint; unauthenticated external access is blocked by the internal API gate. |
| `AGD_CORS_ORIGINS` | `*` | Comma-separated allowed CORS origins. Restrict to your dashboard origin(s) for a browser-facing deployment. |
| `AGD_MAX_REQUEST_BYTES` | `26214400` | Max request body size in bytes (25 MiB). Larger requests get `413`. |
| `AGD_CSP` | (none) | Optional `Content-Security-Policy` header. Opt-in: the editors load from CDNs and the music tab embeds arbitrary origins, so a strict policy can break features. A recommended starting policy is in `.env.example`. |

Baseline response headers (`X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, and `Strict-Transport-Security` over HTTPS) are sent automatically. Sensitive data files (`.secret_key`, `secrets.json`, `config.json`, `secret_scope.json`, `users.json`, `dashboard.db`) are `chmod 600` at startup.

## Local Account Login

AgeniusDesk ships with built-in account login. On first run the browser forces you to create an owner account (email + password), then requires sign-in on every visit. The email is the login identity and is also used for password recovery. The owner account is an `admin`. Accounts may optionally enable TOTP two-factor (any authenticator app); recovery codes are issued once at enrollment.

An edge identity (Cloudflare Access / trusted reverse proxy header) can satisfy the gate without a local account only when `AGD_TRUST_EDGE_AUTH=true`. Do not enable that on a directly reachable port; clients can spoof those headers. Automation can authenticate with `AGD_ADMIN_TOKEN` as a bearer token.

**Forgot password:** the sign-in screen has a "Forgot password?" link. It issues a single-use, time-limited reset link to the account email. Delivery requires SMTP (below); if SMTP is not configured the link is written to the container log so a self-hosted operator can still recover access. Completing a reset signs out all other sessions.

| Variable | Default | Description |
|----------|---------|-------------|
| `AGD_DISABLE_LOGIN` | `false` | Turn browser login OFF entirely (no account, no session). Dev/localhost only; logged loudly at startup. Anyone who can reach the port gets full access. |
| `AGD_SESSION_TTL_DAYS` | `14` | Sliding session lifetime; extended on activity. |
| `AGD_SESSION_ABSOLUTE_DAYS` | `30` | Hard cap on a session regardless of activity. |
| `AGD_LOGIN_MAX_ATTEMPTS` | `8` | Failed logins (per email and per IP) before a temporary lockout. |
| `AGD_LOGIN_LOCKOUT_MINUTES` | `15` | Lockout duration after too many failures. |
| `AGD_PASSWORD_MIN_LENGTH` | `12` | Minimum password length for new accounts, resets, and changes. |
| `AGD_PASSWORD_REQUIRE_UPPER` | `true` | Require an uppercase letter. |
| `AGD_PASSWORD_REQUIRE_LOWER` | `true` | Require a lowercase letter. |
| `AGD_PASSWORD_REQUIRE_NUMBER` | `true` | Require a digit. |
| `AGD_PASSWORD_REQUIRE_SYMBOL` | `true` | Require a non-alphanumeric symbol. |
| `AGD_PASSWORD_RESET_TTL_MINUTES` | `30` | Lifetime of a password-reset link. |

The setup, reset, and change-password screens show a live checklist of these rules; the same policy is enforced server-side.

### Email (SMTP) for password reset

Configure SMTP to deliver password-reset links. When `AGD_SMTP_HOST` is blank, AgeniusDesk logs the reset link instead of emailing it (a deliberate fallback for self-hosted installs without a mail server).

| Variable | Default | Description |
|----------|---------|-------------|
| `AGD_SMTP_HOST` | (none) | SMTP server hostname. Unset = log reset links instead of sending. |
| `AGD_SMTP_PORT` | `587` | SMTP port. |
| `AGD_SMTP_USER` | (none) | SMTP username (or API-key user). |
| `AGD_SMTP_PASSWORD` | (none) | SMTP password. |
| `AGD_SMTP_FROM` | `AGD_SMTP_USER` | From address on outgoing mail. |
| `AGD_SMTP_STARTTLS` | `true` | Issue STARTTLS before authenticating. |
| `AGD_PUBLIC_URL` | (request origin) | Public base URL for links in emails, e.g. `https://app.example.com`. Set this behind a proxy. |

Passwords are hashed with PBKDF2-HMAC-SHA256 (600k iterations, per-user random salt; legacy hashes are upgraded transparently on next login). Sessions are stored server-side as a SHA-256 of the token, so a database leak cannot be replayed. The session cookie is `HttpOnly`, `SameSite=Strict`, and `Secure` over HTTPS; mutations carry a double-submit CSRF token. Roles are `viewer < operator < admin`; v1 enforcement is coarse per route group (read surfaces require any signed-in user, the n8n and container control surfaces require `operator`, and admin/secrets require `admin`).

## Secret Resolution

All configuration values that reference secrets use the following resolution order:

1. **Environment variable**; checked first (e.g., `ANTHROPIC_KEY`, `OPEN_AI_KEY`)
2. **Encrypted secret store**; if not found in env, checked in `data/secrets.json` using `$VAR_NAME` references

Example:
```bash
# In .env:
ANTHROPIC_KEY=sk-ant-...

# Or in Secrets Store (UI):
Name: ANTHROPIC_KEY
Value: sk-ant-...
```

Both work. Environment variables take precedence.

## Example .env File

```bash
# Core
PORT=3000
SECRET_KEY=  # Leave blank to auto-generate

# Authentication
AGD_REQUIRE_AUTH=false

# AI Provider (choose one)
ANTHROPIC_KEY=sk-ant-...
# OPEN_AI_KEY=sk-...
# OPEN_ROUTER_KEY=...
# OLLAMA_URL=http://localhost:11434

# Optional RAG
# QDRANT_URL=http://localhost:6333
# QDRANT_API_KEY=...

# Optional Notifications
# SLACK_WEBHOOK_URL=https://hooks.slack.com/...
# DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...

# Docker (if needed)
# AGD_HOST_ALIASES=192.0.2.10,myhost.local
# AGD_PUBLIC_HOST=ageniusdesk.example.com
```

## TLS and Self-Signed Certificates

If your n8n instance uses a self-signed certificate (common on private LANs):

```bash
AGD_TLS_VERIFY=false
```

**Warning:** This disables certificate verification for all outbound connections, including to your n8n instance. Only use this on trusted, private networks.

For production deployments, use proper TLS certificates (Let's Encrypt, etc.).

## Secrets Store

Sensitive values can be stored encrypted in the dashboard:

1. **Settings > Secrets > Add Secret**
2. Name: `MY_API_KEY`, Value: `sk-...`
3. Reference anywhere as `$MY_API_KEY`

The secret is encrypted at rest using Fernet (AES-128-CBC + HMAC-SHA256) and stored in `data/secrets.json`. Only values in the `.env` file are exposed to n8n instances via API keys; custom secrets stay isolated to the dashboard.

## Backup and Recovery

**Critical files to back up:**

- `data/.secret_key`; master key for decryption (mode 600, auto-generated if missing)
- `data/secrets.json`; encrypted secret store
- `data/users.json`; account credentials (PBKDF2 hashes, encrypted TOTP secrets)
- `data/dashboard.db`; SQLite database (errors, messages, notes, login sessions)

If using PostgreSQL:
- Backup the PostgreSQL database directly (pg_dump, etc.)

Losing `data/.secret_key` makes all encrypted values unrecoverable. Keep it safe.
