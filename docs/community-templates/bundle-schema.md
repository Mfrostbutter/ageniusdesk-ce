Last updated: 2026-05-21

# Bundle template schema (multi-container community templates)

Single-container community templates are documented by example in `example-uptime-kuma.json`. This page covers the multi-container variant. Both live in `data/templates/*.json` and load fresh on every `GET /api/containers/templates`.

A template is a bundle if and only if the JSON has a top-level `containers: [...]` array. The 8 built-in single-container templates and any single-container community JSON keep working unchanged.

## Top-level shape

```json
{
  "id": "infisical",
  "name": "Infisical",
  "category": "secrets",
  "icon": "🔐",
  "description": "Self-hosted secret server with bundled Postgres and Redis.",
  "documentation_url": "https://infisical.com/docs/self-hosting/overview",
  "fields": [ ... ],
  "auto_secrets": ["db_password", "encryption_key", "auth_secret"],
  "containers": [ ... ]
}
```

| Key | Required | Purpose |
|---|---|---|
| `id` | yes | Unique template id, becomes the bundle name prefix (`agd-<id>-<instance>-<member>`). |
| `name` | yes | Tile label. |
| `category` | yes | Tile grouping (e.g. `secrets`, `database`). |
| `icon` | no | Emoji shown on the tile. |
| `description` | no | Tile description. |
| `documentation_url` | no | "Docs" link on the tile. |
| `fields` | yes | The user-visible config form (same shape as single-container templates). At minimum include `instance_name` and any user-tunable ports. |
| `auto_secrets` | no | List of field names that should be auto-minted if the user leaves them blank. Persisted to `data/template_state/bundle_<id>/<instance>.json` so redeploys keep the same DB password / encryption key. |
| `containers` | yes | The bundle members (see below). |

## Container spec

Each entry in `containers`:

```json
{
  "name": "db",
  "role": "service",
  "depends_on": [],
  "expose_port": "{port}",
  "volumes": {"data": "agd-bundle-{template_id}-{instance_name}-db"},
  "config": { Docker create-container API payload }
}
```

| Key | Required | Purpose |
|---|---|---|
| `name` | yes | Member name. Becomes part of the container name and the in-bundle DNS alias. Unique within the bundle. |
| `role` | yes | `primary` or `service`. Exactly one `primary` per bundle. The primary's `expose_port` is the bundle's headline URL. |
| `depends_on` | no | List of sibling `name`s that must start before this one. Topological sort fixes the deploy order. Cycles are rejected at load time. |
| `expose_port` | only on primary | Host port the bundle's Open URL is built against. May be a field substitution like `"{port}"`. |
| `volumes` | no | Map of `local_key -> fully_qualified_volume_name`. The volume name supports `{instance_name}` and other field substitutions. Reference these from `config["HostConfig"]["Binds"]` via `{volume:local_key}`. |
| `config` | yes | Direct Docker create payload (Image, Env, HostConfig, etc.). Field substitutions are applied to the stringified config before parsing back to JSON. |

## Field substitution

These tokens are replaced in any string inside `config`, `volumes`, and `expose_port`:

| Token | Resolves to |
|---|---|
| `{<field_id>}` | The user-supplied value (or auto-minted secret) for that field. |
| `{instance_name}` | The normalised instance name (lowercased, hyphenated). |
| `{volume:<local_key>}` | The fully-qualified volume name declared in this spec's `volumes` map. |
| `{bundle_host:<sibling_name>}` | The sibling member's name. The deployer attaches every member to a bundle-local user-defined bridge network, so siblings resolve by name via Docker's embedded DNS. |

## Runtime contract

When the user submits the deploy form, the deployer:

1. Mints any `auto_secrets` not supplied by the user. Persists them to `data/template_state/bundle_<template_id>/<instance_name>.json` so future redeploys reuse the same values (matters for the DB password specifically: if it rotates between deploys, the existing Postgres volume's credentials become unreadable).
2. Calls the bundle builder with the merged field dict.
3. Normalises the result to a `list[ContainerSpec]` and runs `validate_bundle` (exactly-one primary, no cycles, no dangling `depends_on`, no duplicate names).
4. Topologically sorts the specs.
5. Creates the bundle network (`agd-bundle-<template_id>-<instance_name>-net`, driver `bridge`).
6. For each spec in topo order: pulls the image, ensures volumes, removes any stale container with the same name, creates with bundle labels + network attachment, starts.
7. Persists the full field set to template state for `recreate_bundle` to replay later.

Every member carries these labels:

```
ageniusdesk.managed       = true
ageniusdesk.template      = <template_id>
ageniusdesk.instance      = <instance_name>
ageniusdesk.bundle        = <template_id>:<instance_name>
ageniusdesk.bundle.role   = primary | service
ageniusdesk.bundle.member = <name>
com.docker.compose.project = agd-bundle-<template_id>-<instance_name>
```

The compose-project label is purely for UI grouping (the running list groups by `com.docker.compose.project` already); the `ageniusdesk.bundle` label is the source of truth for membership.

## API surface (added when bundles ship)

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/containers/bundle/<template_id>:<instance_name>` | Member list + metadata. |
| `DELETE` | `/api/containers/bundle/<template_id>:<instance_name>?remove_volumes=<bool>` | Cascade destroy all members + bundle network. |
| `POST` | `/api/containers/bundle/<template_id>:<instance_name>/recreate` | Pull latest images and recreate every member in topo order. Returns a `deploy_id` for the SSE progress stream. |

The existing `POST /api/containers/deploy` route dispatches automatically: when the requested template is bundle-shaped, the deployer routes to `deploy_bundle()` and emits the bundle SSE shape.

## SSE event additions

The progress stream gets one new event type for bundles:

```json
{"event": "bundle_step", "current": 2, "total": 3, "container_name": "redis"}
```

The `done` event for a bundle deploy carries:

```json
{
  "event": "done",
  "bundle": true,
  "bundle_id": "infisical:prod",
  "template_id": "infisical",
  "primary_url": "http://host:8090",
  "container_id": "<primary short id>",
  "container_name": "<primary container name>",
  "url": "<primary url>",
  "containers": [
    {"name": "...", "id": "...", "url": "...", "role": "primary|service", "member": "..."}
  ]
}
```

Single-container deploys keep their existing flat done event. The frontend branches on `bundle === true`.

## Reference implementation

See `data/templates/infisical.json` for a complete Infisical (app + postgres + redis) bundle.

Detailed design rationale, decision table, and verification checklist: `docs/specs/multi-container-templates-2026-05-21.md`.
