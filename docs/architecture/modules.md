# Module System

AgeniusDesk CE is built as a set of self-registering modules. Every feature area (errors, the AI assistant, n8n proxying, Docker management, knowledge sources, and so on) lives in `backend/modules/{id}/`, ships a `manifest.json`, and exposes a `router`. At startup the app scans the module directories, loads each manifest, gates on compatibility, imports the package, and mounts its router. The same machinery loads community modules from the data volume, so a third party can extend the dashboard without forking it. This page covers discovery and registration, the manifest schema, version gating, secrets surfacing, frontend nav contribution, built-in vs community load order, failure handling, and how to add a module. The host app wiring is described in [Architecture Overview](overview.md).

## Discovery and registration

The entry point is `register_modules(app)` in `backend/modules/__init__.py`, called once at import time from `backend/main.py`:

```python
modules = register_modules(app)
logger.info("Registered %d modules: %s", len(modules), ", ".join(modules))
```

It clears the live registry, then runs two passes (built-ins, then community), and finally returns the list of ids that ended up `loaded` or `missing_secrets` (a still-functional state). Per module, both passes follow the same shape:

1. Load `manifest.json` from the module directory via `load_manifest()`. Built-ins without one fall back to `synthesize_builtin_manifest()` (a minimal manifest derived from the directory name). Community modules with no valid manifest are skipped with a warning.
2. Check `min_app_version` against the running app version with `is_compatible()`. If incompatible, register a `RegistryEntry` with `status="incompatible"` and stop, do not import.
3. Import the Python package (`importlib.import_module`). Built-ins import as `backend.modules.{name}`; community modules have their parent dir (`data/modules`) inserted onto `sys.path` first so they import as top-level packages by id.
4. If the imported module has a `router` attribute, call `app.include_router(mod.router)`, compute missing secrets with `check_secrets()`, and register the entry with `status="loaded"` (or `"missing_secrets"` if any required secret is absent).
5. A built-in with no `router` is silently skipped (`debug` log). A community module with no `router` is recorded as `failed`.

The registry itself lives in `backend/module_registry.py` as an in-memory dict (`_registry: dict[id -> RegistryEntry]`) accessed via `register()`, `get_registry()`, `unregister()`, and `clear_registry()`. It is the single source of truth the module-manager UI and the installer query, so neither has to walk the filesystem.

`APP_VERSION` is read once from `pyproject.toml` by `_read_app_version()` (a regex parse so it works on Python 3.10, which lacks `tomllib`); it falls back to `0.0.0` rather than crashing.

## `manifest.json` schema

A manifest deserializes into the `ModuleManifest` pydantic model in `backend/module_registry.py`. Unknown fields are tolerated by pydantic's defaults; only `id` and `name` are effectively required (the rest have defaults).

| Field | Type | Default | Meaning |
|---|---|---|---|
| `id` | string | (required) | Unique module id; also the directory name and registry key |
| `name` | string | (required) | Human label shown in the module manager UI |
| `version` | string | `"1.0.0"` | Module's own semantic version |
| `min_app_version` | string | `"0.0.0"` | Minimum AgeniusDesk version required; drives compatibility gating |
| `description` | string | `""` | Shown in the module manager |
| `author` | string | `""` | Author name |
| `author_url` | string | `""` | Author link |
| `repo` | string | `""` | Source repo (`owner/repo` or URL); used by the installer for updates |
| `license` | string | `""` | License identifier |
| `routes_prefix` | string | `""` | Declared API prefix, e.g. `/api/admin`; an audit-trail hint, the actual prefix comes from the router |
| `python_entry` | string | `"__init__.py"` | Entry file convention |
| `secrets_required` | list of `SecretRequirement` | `[]` | Declared secrets the module needs; drives green/red surfacing and the install prompt |
| `frontend` | `FrontendDecl` or null | null | Nav entry + view/script contributions |
| `builtin` | bool | `false` | Marks a built-in module |
| `homepage` | string | `""` | Optional homepage link |
| `capabilities` | `Capabilities` or null | null | Declared capability surface (network/filesystem/subprocess/env); the scanner reconciles it against the code. `null` means "declares nothing" |
| `signature` | string | `""` | Optional detached manifest signature (base64). Field shape is reserved; verification is best-effort/additive and key distribution is out of scope for now. Absent = unsigned |

`Capabilities` (all optional; an absent block declares nothing, so any detected capability is treated as undeclared):

| Field | Type | Default | Meaning |
|---|---|---|---|
| `network` | `{ enabled: bool, hosts: [string] }` | `enabled=false` | Whether the module makes outbound calls, and the host allowlist (globs like `*.youtube.com`). `enabled` with an empty `hosts` means "any host" and is itself a HIGH finding |
| `filesystem` | `{ write_paths: [string] }` | `[]` | Paths under `data/` the module writes; writes elsewhere are findings |
| `subprocess` | bool | `false` | Whether the module spawns child processes |
| `env` | list of string | `[]` | Environment variable keys the module reads, beyond `secrets_required` |

Example with capabilities (a YouTube research module):

```json
"capabilities": {
  "network": { "enabled": true, "hosts": ["*.youtube.com", "api.openai.com"] },
  "filesystem": { "write_paths": ["workspace/research"] },
  "subprocess": false,
  "env": ["WHISPER_URL"]
}
```

`SecretRequirement`:

| Field | Type | Default | Meaning |
|---|---|---|---|
| `key` | string | (required) | Secret name as referenced in the secrets store, e.g. `ANTHROPIC_KEY` |
| `description` | string | `""` | Shown next to the key in the UI |
| `required` | bool | `true` | If true and absent, the module loads as `missing_secrets` |

`FrontendDecl`:

| Field | Type | Default | Meaning |
|---|---|---|---|
| `nav` | `NavEntry` or null | null | A single sidebar nav entry contributed by the module |
| `views` | list of string | `[]` | View ids/paths the module provides |
| `scripts` | list of string | `[]` | Extra scripts to load |

`NavEntry`:

| Field | Type | Default | Meaning |
|---|---|---|---|
| `label` | string | (required) | Nav label |
| `icon` | string | `""` | Icon id |
| `view` | string | `""` | For built-ins: a view id registered in `app.js`. For community modules: a relative HTML path served at `/modules/{id}/static/{view}` |

Minimal built-in example (`backend/modules/admin/manifest.json`):

```json
{
  "id": "admin",
  "name": "Admin",
  "version": "1.0.0",
  "min_app_version": "0.1.0",
  "description": "User management, config, and secrets admin endpoints.",
  "author": "AgeniusDesk",
  "routes_prefix": "/api/admin",
  "builtin": true
}
```

Manifest with secrets and a nav entry (`backend/modules/assistant/manifest.json`):

```json
{
  "id": "assistant",
  "name": "AI Assistant",
  "version": "1.0.0",
  "min_app_version": "0.1.0",
  "description": "LLM-powered assistant + Code Lab integration. Supports Anthropic, OpenAI, OpenRouter, Ollama.",
  "author": "AgeniusDesk",
  "routes_prefix": "/api/assistant",
  "builtin": true,
  "secrets_required": [
    { "key": "ANTHROPIC_KEY", "description": "Anthropic API key", "required": false },
    { "key": "OPEN_AI_KEY", "description": "OpenAI API key", "required": false },
    { "key": "OPEN_ROUTER_KEY", "description": "OpenRouter API key", "required": false }
  ],
  "frontend": {
    "nav": { "label": "Assistant", "icon": "robot", "view": "assistant" }
  }
}
```

## `min_app_version` gating

`is_compatible(min_app_version)` in `backend/module_registry.py` compares version tuples: `version_tuple(min_app_version) <= version_tuple(APP_VERSION)`. `version_tuple` splits on `.`, takes the leading integer of each segment (stripping any `-prerelease` suffix), and substitutes `0` for non-numeric segments. Parsing is permissive: a malformed version returns `True` (treated as compatible) rather than failing the load.

A module that fails the check is not imported. It is registered with `status="incompatible"` and an `error` of `Requires app version >= {min_app_version}`, so the module manager can show it as needing an upgrade. The community installer applies the same check before committing an install and refuses an incompatible module outright.

## Secrets surfacing

`check_secrets(manifest)` reads the dashboard secrets store via `load_secrets()` and returns the list of `secrets_required` keys that are both `required=true` and absent. A module with missing required secrets still imports and mounts its router, but registers as `status="missing_secrets"` with `missing_secrets` populated, so the UI can render a red indicator and prompt the operator to supply them. Optional secrets (`required=false`, as the assistant declares for its three provider keys) never block loading; they document what the module can use. Manifests do not store secret values; only the key names. Resolution of the actual value follows the store's order (environment first, then the encrypted store).

## Frontend nav contribution

Modules contribute sidebar entries through `manifest.frontend.nav`. The `modules` module exposes `GET /api/modules/nav` (`backend/modules/modules/router.py`), which returns one entry per `loaded`/`missing_secrets` module that declares a nav, including:

- `module_id`, `source` (`builtin` or `community`)
- `label`, `icon`, `view`
- `static_base`: `/modules/{id}/static/` for community modules, `null` for built-ins

The frontend appends these to its hardcoded built-in nav. Built-in module views resolve to a view id registered in `app.js`; community module views are HTML/JS files served from the data volume by `static_router.py` at `/modules/{module_id}/static/{file_path}`, which resolves paths inside the module's own directory and rejects traversal. Built-in frontends are bundled into `frontend/` and are not served through that route. See [Frontend](frontend.md).

## Built-in vs community: load order and directories

| | Built-in | Community |
|---|---|---|
| Directory | `backend/modules/{id}/` (`BUILTIN_DIR`) | `data/modules/{id}/` (`COMMUNITY_MODULES_DIR`) |
| Load order | First | Second |
| Import path | `backend.modules.{id}` | top-level `{id}` (parent dir injected onto `sys.path`) |
| Manifest required | No (synthesized fallback) | Yes (skipped if absent/invalid) |
| Installed via | Ships in the repo | `POST /api/modules/install` (installer) |
| `source` in registry | `"builtin"` | `"community"` |

Within each pass, directories are processed in sorted order, and any whose name starts with `_` is skipped. The community pass is skipped entirely when `data/modules/` does not exist (the first-boot case).

The CE built-in set includes: `admin`, `assistant`, `auth`, `dashboard_mcp`, `docker_mgr`, `errors`, `health`, `insights`, `knowledge`, `messages`, `modules`, `n8n_credentials`, `n8n_proxy`, `notes`, `player`, `public_api`, `themes`, `webhooks`.

## Community module installation (two-phase: inspect, then install)

Install is split into a dry-run **inspect** and a consented **install**, both driven by `backend/modules/modules/installer.py` through `router.py`.

`POST /api/modules/inspect` `{ repo, ref }`:

1. Parse the repo spec (`owner/repo` or a GitHub URL) and download the tarball for a tag, branch, or SHA via the GitHub tarball API, following the redirect to `codeload.github.com` and recording the resolved commit SHA.
2. Extract to a staging dir under `data/modules/`. Extraction is defensive: it validates every tar member before writing anything, rejecting symlinks and hardlinks, non-regular members (devices, fifos), absolute paths, `..` traversal, and any member whose resolved target escapes the staging directory. On Python 3.12+ it additionally applies the stdlib `data` filter as a second layer.
3. Read the `manifest.json` and run the **static AST scanner** (`scanner.py`) over every `.py` file (see below), then **discard the staging dir**. Nothing is registered or persisted.
4. Return `{ manifest, capabilities, scan_report, resolved_sha, compatible }` for the operator to review.

`POST /api/modules/install` `{ repo, ref, resolved_sha, consent }`:

1. Re-download and re-extract, then reject the install if the ref now resolves to a different commit than the `resolved_sha` the operator inspected (swapped-tag guard).
2. Re-validate the manifest and `min_app_version` compatibility, re-run the scanner (same commit, same code, same findings), and enforce **consent server-side**: a CRITICAL finding requires the operator to have typed the module id; a HIGH finding requires an explicit acknowledgement. The gate is never derived from client-submitted findings.
3. Move the staged dir to `data/modules/{manifest.id}/`, record the install in `data/modules-lock.json` (repo, pinned ref, resolved SHA, timestamp, version, `approved_by`, `scan_max_severity`), and append an audit row to the `module_installs` SQLite table (`module_id`, `repo`, `ref`, `resolved_sha`, `capabilities_json`, `scan_summary`, `scan_max_severity`, `approved_by`, `approved_at`). The audit write is best-effort and never fails an already-completed install.

The install returns `restart_required: True`; the new module is mounted on the next `register_modules` pass at app start. `uninstall(module_id)` removes the directory and the lock entry but leaves any secrets in the store for the operator to clean up separately.

### Monorepo support (discover + subdir install)

A single repo can hold many modules (one repo, many modules), so the install flow starts with discovery:

- `POST /api/modules/discover` `{ repo, ref }` downloads the repo and lists every installable module without registering anything. It recognizes both a single-module repo (a `manifest.json` at the repo root, `path: ""`) and a monorepo (`modules/<id>/manifest.json`, or any `<id>/manifest.json` one or two levels deep), returning `{ id, name, version, description, path, compatible }` per module.
- `inspect` and `install` then take an optional `path` (the module's subdir within the repo, blank = root). The path is traversal-safe: it must stay inside the staged repo and contain a `manifest.json`. Only that subtree is scanned and, on install, lifted out to `data/modules/{id}/`; sibling modules in the same repo are left untouched, and the staging copy of the rest of the repo is discarded.

The module manager UI reflects this: the install panel's **Discover** button lists the repo's modules, and each has its own **Inspect** button that runs the scan + consent + install for that one module. The recommended monorepo layout is `modules/<id>/` with each module self-contained (no imports from sibling modules or shared repo-root code, since only the one subdir is installed).

### Static AST scanner

`scanner.py` parses each `.py` file with the stdlib `ast` module and emits severity-ranked findings; it never imports or executes module code. The headline output is the **declared-vs-detected diff**: a capability the code uses but the manifest did not declare surfaces as a HIGH "undeclared capability."

| Severity | Examples detected |
|---|---|
| CRITICAL | `eval`/`exec`/`compile`, `os.system`/`os.popen`, dynamic `__import__`/`importlib.import_module` with a non-literal name, `pickle`/`marshal` loads, `ctypes` |
| HIGH | undeclared network imports/calls, calls to a host outside the declared allowlist, raw sockets, `subprocess` when undeclared or `shell=True`, writes outside declared `write_paths`, reads of undeclared env vars, references to the secret store |
| MEDIUM | out-of-tree file reads, dynamic `getattr`/`setattr` on imported modules, cross-module references, large opaque base64/hex literals |
| INFO | over-declaration (a declared capability the code never uses) |

> **Heuristic review, not a sandbox.** A static scan of code that runs in-process cannot contain a determined author (`getattr(__import__('os'), 'system')`, base64-then-`exec`, runtime-fetched payloads all bypass it). The scan catches low-effort or accidental danger, forces an explicit consent moment, and records what was approved. Absence of findings is not a safety guarantee. The report carries its own limitations text, and every UI surface says the same. There is no "scanned and safe" badge.

Security posture is explicit and limited: community modules run in-process with full Python access and no sandbox. Out-of-process isolation is the deferred real boundary (see [Security](security.md)). Only `secrets_required` keys are surfaced for the operator to supply; the full `.env` is not auto-injected. Install only from sources you trust.

## How a failed community module is recorded, not fatal

The loader never lets a single bad module take down the app. In `_register_community` (and likewise `_register_builtin`), the import and router mount are wrapped in try/except. On any exception the module is registered with `status="failed"` and the exception string in `error`, and a warning is logged. A community module that imports but lacks a `router` is recorded as `failed` with `error="Module has no `router` attribute"`. Either way the module manager UI shows the module as broken with its error, rather than the module silently disappearing or the process crashing. The full status vocabulary is `loaded`, `failed`, `incompatible`, `missing_secrets`, `disabled`.

## Adding a new module

For a built-in:

1. Create `backend/modules/{id}/` with an `__init__.py` that exposes a `router` (a FastAPI `APIRouter`, conventionally prefixed `/api/{id}`).
2. Add `backend/modules/{id}/manifest.json` with at least `id` and `name`. Set `min_app_version`, declare any `secrets_required`, and add a `frontend.nav` entry if the module has a UI view.
3. If it surfaces a UI, register the view in the frontend (`app.js`) and add the nav `view` id to the manifest.
4. Restart the app. `register_modules` discovers and mounts it; confirm it shows as `loaded` in `GET /api/modules`.

For a community module, package the same structure (a top-level package named for the `id`, with `__init__.py` exposing `router`, plus `manifest.json` at the repo root) in a GitHub repo and install it via `POST /api/modules/install`. Use a real `min_app_version` so older dashboards reject it cleanly, and declare every credential under `secrets_required` so the operator is prompted rather than left guessing.

## See also

- [Architecture Overview](overview.md)
- [Data Model](data-model.md)
- [Authentication & RBAC](auth.md)
- [Frontend](frontend.md)
- [API Reference](api.md)
- [Security](security.md)
- User guide: [../guide/](../guide/)
