# Containers

The Containers view is a Docker control plane built into AgeniusDesk. It lists every container on the Docker host, deploys new services from built-in templates in one click (with auto-generated passwords and live progress), and gives you start/stop/logs/recreate/destroy lifecycle controls. It also deploys multi-container bundles with shared networking and loads community templates from a drop-in directory.

This view talks to the Docker daemon directly, which is root-equivalent. Read the security note at the bottom and [../architecture/security.md](../architecture/security.md) before exposing it.

Related: deployed n8n containers can be registered as instances; see [Executions & Errors](errors.md) for what to do once an n8n instance is connected.

## Prerequisite: Docker socket

The dashboard reaches Docker through `/var/run/docker.sock`, which must be mounted into the dashboard container. If it is not, the view shows "Docker daemon unreachable" and `GET /api/containers/status` returns `{"reachable": false}`. Mounting the socket grants the dashboard full control of the host's Docker, which is **root-equivalent on the host**.

## Container list

The table lists all containers (running first, then alphabetical). The header summary shows running / stopped / image counts and the Docker version. Controls:

- **Group by project**: groups rows by Docker Compose project (standalone containers under "Standalone").
- **Filter bar**: All, Running, and one button per compose project (with `running/total` counts).
- **Refresh**: reloads now. The list also auto-refreshes every 15 seconds.

Each row shows a state dot, name and short id, image, published ports (`hostPort->containerPort`), and status.

## Lifecycle actions

Per-row action buttons and a **More** (`⋯`) dropdown:

| Action | Where | Endpoint | Notes |
|---|---|---|---|
| Start | row (stopped) | `POST /api/containers/{id}/start` | |
| Stop | row (running) | `POST /api/containers/{id}/stop` | |
| Restart | row (running) | `POST /api/containers/{id}/restart` | |
| Pause / Unpause | (valid actions) | `POST /api/containers/{id}/{action}` | |
| Open | row | n/a (opens browser tab) | Enabled when the container exposes an HTTP-shaped port |
| Logs | row | `GET /api/containers/{id}/logs` (SSE) | See below |
| Inspect | More | `GET /api/containers/{id}/inspect` | Raw inspect JSON in a modal |
| Recreate (pull latest) | More | `POST /api/containers/{id}/recreate` | Pulls latest image, recreates with the same config; brief downtime |
| Register as instance | More (n8n only) | n/a | Pre-fills the Add Instance dialog |
| Recreate bundle | More (bundle members) | `POST /api/containers/bundle/{id}/recreate` | Pulls and recreates every member in dependency order |
| Destroy | More | `DELETE /api/containers/{id}` | Confirm dialog; optional volume deletion |

Valid container actions are `start`, `stop`, `restart`, `pause`, `unpause`.

### Logs

Clicking **Logs** opens a streaming panel that tails 300 lines via Server-Sent Events. Toggle **Follow** to stream new lines live. **Clear** wipes the panel display; **✕** closes the stream. The panel caps display at ~2000 lines to keep the DOM light.

### Recreate (rolling update)

**Recreate (pull latest)** pulls the newest image for the container's current tag, stops and removes the old container, and recreates it with the same image, env, labels, and host config. There is brief downtime. Progress streams in the deploy panel (same SSE protocol as a fresh deploy). Data volumes are preserved.

### Destroy

Destroy permanently removes a container. The confirm dialog offers:

- **Also delete associated volumes** (managed containers and bundle members): deletes the named data volumes too. All data is lost.
- **Destroy entire bundle** (bundle members): cascade-destroys every member and tears down the bundle network. On by default for bundle members.

When a destroyed container's published ports match a registered n8n instance, that instance is automatically de-registered (the toast names which instances were removed).

## Deploying from a template

Click **+ Deploy** to open the deploy panel, a three-step flow.

1. **Choose a template**: a tile grid grouped by category. Each tile shows an icon, name, short description, a community badge if applicable, and a "running" marker if an instance of that template is already up. Tiles may link to template docs.
2. **Configure**: a form rendered from the template's fields (text, password, number, select). Defaults are pre-filled. Host ports that browsers refuse to open (Chrome's ERR_UNSAFE_PORT list) and out-of-range ports are rejected up front. Click **Deploy**.
3. **Progress**: live deploy steps stream over SSE. On success a result banner offers **Open instance**, **Add to AgeniusDesk** (register it as an n8n instance), and **Deploy another**.

### Deploy progress (SSE)

The deploy returns a `deploy_id`; the browser connects to `GET /api/containers/deploy/{deploy_id}/progress`. Each event is a JSON object:

```json
{"event": "step", "message": "Pulling n8nio/n8n:latest…", "detail": "This may take a few minutes on first run."}
{"event": "bundle_step", "current": 1, "total": 2, "container_name": "db"}
{"event": "done", "container_id": "abc123def456", "url": "http://host:5678"}
{"event": "error", "message": "Image pull failed: ..."}
```

The deploy lifecycle on the server is: pull image -> create named volume(s) (reused if they already exist) -> remove any stale container with the same name -> create -> start. Containers deployed this way are named `agd-<instance_name>` and carry `ageniusdesk.managed`, `ageniusdesk.template`, and `ageniusdesk.instance` labels.

### Auto-generated passwords

The one-click n8n quick deploy (from the dashboard welcome card) generates a strong admin password client-side with `crypto.getRandomValues`, pre-fills every field, and surfaces the password in the form so you can copy it before deploying. For other templates you supply the password in the config form; leaving it blank is allowed only where the template marks it optional. n8n's encryption key is generated once per instance and persisted outside the data volume, so redeploys reuse it and do not corrupt existing encrypted credentials.

## Built-in templates

Eight templates ship in the box:

| Template | Image | Category | Notes |
|---|---|---|---|
| n8n | `n8nio/n8n:latest` | automation | Basic auth, persistent volume, per-instance encryption key, optional webhook URL |
| PostgreSQL | `postgres:16` | database | DB name, user, password; persistent volume |
| MongoDB | `mongo:7` | database | Root user auto-provisioned on first boot; initial DB |
| Redis | `redis:7-alpine` | database | Append-only persistence on by default; optional password |
| Qdrant | `qdrant/qdrant` | ai | Vector DB; REST on base port, gRPC on port+1 |
| Ollama | `ollama/ollama` | ai | Local LLMs; pull models after deploy |
| Flowise | `flowiseai/flowise` | automation | No-code AI workflow builder; user/password |
| MinIO | `minio/minio` | storage | S3-compatible; API on base port, console on +1 |

Each template declares typed fields (instance name, host port, credentials, etc.) rendered into the config form.

## Multi-container bundles

A bundle template deploys several containers as one unit on a shared, per-bundle bridge network so members resolve each other by name. The deployer mints any shared secrets (DB password, encryption key) once and reuses them on recreate, topologically sorts the members by `depends_on`, creates the bundle network, then deploys each member in order. Bundle ids are `<template_id>:<instance_name>`.

On a mid-bundle failure the deployer does **not** roll back; it emits a partial-bundle error listing what started, what failed, and what remained, so you can fix and recreate. The bundle snapshot is persisted so **Recreate bundle** can replay the same fields against the latest images later.

## Community templates

Drop a JSON file into `data/templates/` (mounted at `/app/data/templates/`) and it appears in the tile grid on the next `GET /api/containers/templates` call. No restart needed. Templates are validated at load time; a malformed file is skipped (with a log warning) rather than breaking the grid.

Two shapes are supported:

- **Single-container**: top-level `container_config` plus `volumes`. Field values substitute into the config via `{field_id}` placeholders.
- **Bundle**: a top-level `containers` array, each entry with `name`, `config`, `volumes`, `depends_on`, `role`, and `expose_port`. Bundle configs additionally resolve `{volume:<key>}` and `{bundle_host:<name>}` placeholders.

Templates may declare `auto_secrets` (minted and persisted automatically) and `post_deploy_hooks` (validated against a known-hook allowlist). The schema and a worked example live in [../community-templates/bundle-schema.md](../community-templates/bundle-schema.md) and `data/templates/example-uptime-kuma.json`.

## Security note

The container manager requires the Docker socket mounted into the dashboard. Anyone who can reach these routes can create, destroy, and inspect containers and delete volumes on the host, which is **equivalent to root on the host machine**. The container API routes require at least the `operator` role. Do not expose AgeniusDesk with the Docker socket mounted on an untrusted network without an authenticated edge in front of it. Full posture: [../architecture/security.md](../architecture/security.md).
