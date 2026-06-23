# Deployment Guide

Runbook for self-hosting AgeniusDesk Community Edition on your own infrastructure.

## Prerequisites

- **Docker Engine** (version 20.10+) and Docker Compose
- **Python 3.10+** (for bare-metal installs only)
- **Git** (for pulling updates)
- **n8n instance(s)** already running and accessible over the network

## Installation

### Docker (Recommended)

Clone the repo and start:

```bash
git clone https://github.com/Mfrostbutter/ageniusdesk-ce.git
cd ageniusdesk-ce
cp .env.example .env
# Edit .env: set AI provider keys, n8n URLs, etc.
docker compose up -d --build
```

The dashboard is now available at `http://localhost:3000`. A setup wizard will walk you through connecting your first n8n instance.

**Important:** The `.env` file and the `data/` volume are created once and live on the host. They survive container restarts and rebuilds. Back them up.

### Bare Metal (Linux/macOS)

For a minimal installation without Docker:

```bash
git clone https://github.com/Mfrostbutter/ageniusdesk-ce.git
cd ageniusdesk-ce
pip install '.[assistant]'
cp .env.example .env
# Edit .env
python -m uvicorn backend.main:app --host 0.0.0.0 --port 3000
```

The backend will start on port 3000. Access at `http://localhost:3000`.

## Configuration

See [docs/CONFIG.md](CONFIG.md) for all environment variables.

**Key configuration for self-hosting:**

```bash
# .env file
PORT=3000
AGD_REQUIRE_AUTH=true              # Enable login (recommended for public deployments)
AGD_TLS_VERIFY=true                # Verify HTTPS certificates (set false only for self-signed certs on private LANs)

# AI Assistant (pick one)
ANTHROPIC_KEY=sk-ant-...           # Or OPEN_AI_KEY, OPEN_ROUTER_KEY, OLLAMA_URL

# Optional
SLACK_WEBHOOK_URL=https://hooks... # For notifications
DISCORD_WEBHOOK_URL=https://...    # For notifications
```

## TLS / HTTPS and Reverse Proxy

For production, put AgeniusDesk behind a reverse proxy with TLS:

### nginx example

```nginx
server {
  listen 443 ssl http2;
  server_name ageniusdesk.example.com;

  ssl_certificate /path/to/cert.pem;
  ssl_certificate_key /path/to/key.pem;

  location / {
    proxy_pass http://localhost:3000;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
  }
}
```

### Caddy example

```caddy
ageniusdesk.example.com {
  reverse_proxy localhost:3000
}
```

## Authentication

By default, AgeniusDesk has no built-in login. For public deployments:

**Option 1: Built-in login** (simple)

```bash
AGD_REQUIRE_AUTH=true
```

This enables local user accounts. Users log in with a username and password (hashed with PBKDF2).

**Option 2: Reverse proxy auth** (advanced)

Let your reverse proxy handle authentication (nginx basic auth, Cloudflare Access, KeyCloak, etc.) and ensure it sets trusted headers. AGD respects `X-Remote-User` from a proxy.

For development (private LAN), leave `AGD_REQUIRE_AUTH=false`.

## Backup and Recovery

**Critical files to back up:**

- `data/.secret_key`: master encryption key (mode 600, auto-generated on first run). Losing this makes encrypted values unrecoverable.
- `data/secrets.json`: encrypted secret store
- `data/dashboard.db`: SQLite database (errors, messages, notes)
- `data/config.json`: encrypted n8n instance list and credentials

**Backup procedure:**

```bash
# Docker
docker compose exec dashboard tar czf /app/data.backup.tar.gz -C /app data/
docker cp ageniusdesk-dashboard-1:/app/data.backup.tar.gz ./

# Or directly (if you know the volume mount path):
tar czf ageniusdesk-backup-$(date +%Y%m%d).tar.gz /path/to/data/

# For PostgreSQL backend:
pg_dump -U user ageniusdesk > ageniusdesk-backup-$(date +%Y%m%d).sql
```

**Recovery:**

```bash
# Docker
docker compose stop
docker compose run --rm dashboard tar xzf /app/data.backup.tar.gz -C /app
docker compose up -d

# Or manually restore the data/ directory, then restart.
```

## Updating

**Docker:**

```bash
git pull
docker compose up -d --build
```

The `--build` flag ensures the image is rebuilt with new code.

**Bare metal:**

```bash
git pull
pip install -U '.[assistant]'
# Restart the uvicorn process (systemd, supervisor, etc.)
```

## Troubleshooting

### Docker socket not found (Containers tab shows error)

AgeniusDesk needs access to the Docker socket to manage containers. The docker-compose.yml mounts `/var/run/docker.sock` into the container.

**Fix:**

```bash
# Make sure Docker is running
sudo systemctl start docker

# On macOS with Docker Desktop, ensure it's open
# On Windows, ensure Docker Desktop is running
```

### Cannot connect to n8n (connection error)

**Inside Docker:**

- Use the n8n container name or LAN IP, not `localhost`
- Example: `http://n8n-prod:5678` (if on the same network) or `http://10.0.1.50:5678` (LAN IP)

**n8n API key:**

- Ensure the key is created in n8n (Settings > API > Create API Key)
- The key is user-scoped; create one after completing n8n's owner setup

**Self-signed certificates:**

- Set `AGD_TLS_VERIFY=false` in `.env` (only for private LANs)

### "Secret key not found" or decryption errors

The `.secret_key` file was lost or corrupted. Once generated, it must be preserved. If you've lost it:

1. All encrypted values in `config.json` and `secrets.json` are unrecoverable.
2. Back up and delete those files.
3. Start fresh: `rm data/config.json data/secrets.json`
4. Reconnect n8n instances and re-enter secrets.

## Data Storage

**Default:** SQLite at `data/dashboard.db` (file-based)

**Optional:** PostgreSQL

```bash
DATABASE_URL=postgresql://user:password@postgres-host:5432/ageniusdesk
```

Migrations run automatically on startup.

## Performance Tips

- For 10+ n8n instances, consider PostgreSQL over SQLite
- Disable unused AI providers in `.env` to reduce startup time
- Set up a Qdrant instance for faster knowledge searches (optional)
- Increase Docker memory limits for the dashboard container if you have large workflows or error logs: `docker-compose.yml` `mem_limit: 2g`

## Getting Help

- Check [docs/CONFIG.md](CONFIG.md) for configuration options
- Open a GitHub issue at https://github.com/Mfrostbutter/ageniusdesk-ce/issues
- Review logs: `docker compose logs dashboard` or `tail -f debug.log`

