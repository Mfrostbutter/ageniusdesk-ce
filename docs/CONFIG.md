# Configuration Reference

AgeniusDesk CE configuration is managed via environment variables. Create a `.env` file in the repo root (copy from `.env.example` as a starting point) and customize the values below.

## Core Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `3000` | Port the dashboard listens on |
| `SECRET_KEY` | Auto-generated | Master key for encrypting secrets. If not set, generated and persisted to `data/.secret_key` (mode 600) on first run. Losing this file makes all encrypted values unrecoverable. Back it up. |
| `AGD_REQUIRE_AUTH` | `false` | Enable built-in dashboard authentication (local user accounts). Set to `true` for public deployments, or front with an auth proxy (nginx, Cloudflare Access). |
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
| `OPEN_ROUTER_KEY` | (none) | OpenRouter API key for 11+ LLM models. |
| `OLLAMA_URL` | `http://localhost:11434` | Local Ollama endpoint for self-hosted models. Inside Docker, use `http://host.docker.internal:11434` or your LAN IP. |

## Knowledge & RAG

| Variable | Default | Description |
|----------|---------|-------------|
| `QDRANT_URL` | (none) | Qdrant vector database endpoint (e.g., `http://localhost:6333`). Optional; enables RAG for knowledge sources. |
| `QDRANT_API_KEY` | (none) | API key for Qdrant (if required). |
| `EMBEDDING_PROVIDER` | `openai` | Embedding model provider: `openai`, `voyage`, `ollama`. |
| `EMBEDDING_MODEL` | `text-embedding-3-small` | Embedding model name (e.g., `text-embedding-3-large` for OpenAI). |
| `SEARCH_PROVIDER` | `none` | Web search provider for assistant: `tavily`, `serper`, `none`. |
| `SEARCH_API_KEY` | (none) | API key for web search provider. |

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

## Security

| Variable | Default | Description |
|----------|---------|-------------|
| `AGD_REQUIRE_AUTH` | `false` | Enforce in-app auth on privileged routes and the `/ws` WebSocket. Leave `false` when an auth proxy fronts the app; set `true` for a bare public bind. Requires `AGD_ADMIN_TOKEN` and/or a trusted edge-auth header. |
| `AGD_ADMIN_TOKEN` | (none) | Bearer token accepted on privileged routes when `AGD_REQUIRE_AUTH=true`. Compared in constant time. |
| `AGD_CORS_ORIGINS` | `*` | Comma-separated allowed CORS origins. Restrict to your dashboard origin(s) for a browser-facing deployment. |
| `AGD_MAX_REQUEST_BYTES` | `26214400` | Max request body size in bytes (25 MiB). Larger requests get `413`. |
| `AGD_CSP` | (none) | Optional `Content-Security-Policy` header. Opt-in: the editors load from CDNs and the music tab embeds arbitrary origins, so a strict policy can break features. A recommended starting policy is in `.env.example`. |

Baseline response headers (`X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, and `Strict-Transport-Security` over HTTPS) are sent automatically. Sensitive data files (`.secret_key`, `secrets.json`, `config.json`, `secret_scope.json`) are `chmod 600` at startup.

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
- `data/dashboard.db`; SQLite database (errors, messages, notes)

If using PostgreSQL:
- Backup the PostgreSQL database directly (pg_dump, etc.)

Losing `data/.secret_key` makes all encrypted values unrecoverable. Keep it safe.
