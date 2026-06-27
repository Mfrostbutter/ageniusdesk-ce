# Spec: Out-of-Process Backend Isolation for Community Modules

Status: DRAFT, revised after adversarial review passes 1 and 2 (Codex, 2026-06-27). Prerequisite id fix LANDED; isolation runtime not yet built.
Date: 2026-06-27
Owner: Michael Frostbutter
Scope: AgeniusDesk Community Edition (host) + ageniusdesk-community-modules (reference consumer)
Release gate: yes. Breaks the backend module contract. Target v0.4 (dual-mode lands in 0.3.0, which is unreleased; current version is 0.2.0).
Companion: `2026-06-26-community-module-security-and-youtube-research.md` (the scan/consent layer this replaces as the real boundary). Frontend half is built (sandboxed iframe + postMessage bridge), unreleased, lands in 0.3.0.

### Revision note (review pass 1)

Addressed all 7 findings from `2026-06-27-out-of-process-backend-isolation-review.md`:
a prerequisite module-id validation fix (CRITICAL, Section 4.1), a tool-free
`assistant.complete` executor (HIGH, Section 5.5b), the worker bootstrap moved
outside the `backend` namespace (HIGH, Section 5.4), `broadcast` downgraded to
optional/future with the missing host->iframe WS relay called out (Section 5.5c,
coupling #3), corrected scanner-severity wording (Section 2), dir-safe notes
validators (Section 5.5a), and version-prose fixes (this block, Section 8).

### Revision note (review pass 2)

Pass 2 reviewed the landed id fix + the revised spec. Addressed all 3 findings:
the landed id policy was hardened for Windows (HIGH: dots forbidden entirely to
kill the `a.` trailing-dot alias, and Windows reserved device names rejected;
code + tests updated, Section 4.1); the `broadcast` scope was made one consistent
v1 truth (MEDIUM: removed the "replace with bridge.broadcast" instruction from
the re-port and proxy sections and moved it out of the main phase plan into a
later phase, Sections 5.1/7/13); and the id-regex prose was aligned to the
enforced rule (LOW, Section 4.1). The only pass-2 blocker (the Windows id gap) is
fixed.

> This document is written to be attacked. Section 3 (enforcement matrix) and
> Section 11 (attack surface) state what is and is NOT a boundary on purpose.
> If a claim here reads as "safe" without a qualifier, treat it as a bug in the
> spec and flag it.

## 1. Goal

A community module's Python must not be able to read host credentials, read or
write the host database, touch the Docker socket, mutate host process state, or
crash the host, except through a small, audited, capability-gated bridge that the
operator consented to. Today it can do all of those with a single `import`.

The frontend boundary shipped in v0.3 (opaque-origin iframe + whitelisted
postMessage bridge). This is the backend half: the last in-process trust
boundary in the product.

Design principle, stated once and load-bearing for the whole spec:

> **The bridge is the contract. The sandbox underneath it is swappable.**

We define the capability bridge (Section 5.5) so that the module's code is ported
exactly once. The confinement mechanism under the bridge can then be upgraded
from "separate subprocess" (v1, light, cross-platform, a raised bar) to
"container-per-module" (the real boundary) without touching module code again.

## 2. Threat model

**Assets to protect.**

- Provider API keys and the secret store (`data/secrets.json`, `data/.secret_key`,
  Fernet master key, all `*_KEY` env values).
- The host database (`data/dashboard.db`): other modules' rows, auth/session
  tables, install audit, errors, notes index.
- The notes vault (`data/workspace/`) outside the module's declared paths.
- The Docker socket (root-equivalent).
- Host process integrity (event loop, global state, monkeypatchable internals).
- Other community modules' code and private data.
- Operator LLM spend.

**Adversaries, in increasing capability.**

1. **Accidental / sloppy module.** Reaches for `backend.*` because the old
   contract said it could, writes outside its lane, over-reads env. The common
   case. v1 stops this cold.
2. **Low-effort malicious module.** Tries the obvious: `from backend.config
   import decrypt_value`, `open("data/secrets.json")`, `get_db()`. Mind the gap in
   what the scanner catches *today*: a direct secret-store read
   (`data/secrets.json`) is HIGH, but a bare `import backend.*` is only INFO
   (`scanner.py:246`), so the import path is not loudly flagged now. v1
   structurally removes the sanctioned in-process pathway, and the contract flip
   upgrades host imports to a HIGH "will not run under isolation" finding
   (Section 6).
3. **Determined malicious module (same-uid).** Knows it is sandboxed and works to
   escape: reads host secrets off disk, reads `/proc/<pid>/environ` of the host or
   sibling workers, fetches a second-stage payload at runtime. **v1 (same-uid
   subprocess) does NOT contain this adversary.** The container tier (Section 5.8)
   does.

**Trust boundary.** The operator already consented to install (scan + typed/ack
consent, audit row). We are not defending against an operator who installs
malware and approves CRITICAL findings; we are (a) making the safe path the
default and the only sanctioned path, (b) shrinking blast radius, (c) giving
crash/fault isolation, and (d) building the architecture on which real
containment (container-per-module) drops in without a second module port.

## 3. Honest framing: the enforcement matrix

This is the most important section. Two tiers ship in sequence under the *same*
bridge. "Enforced" means a determined adversary-3 module is stopped. "Guardrail"
means accidental/low-effort is stopped but a determined same-uid process is not.

| Threat | Subprocess tier (v1, cross-platform) | Container tier (target boundary) |
|---|---|---|
| In-process access to host objects (`get_db`, `decrypt_value`, `manager`, `settings`) | **Enforced** (separate process; not the same interpreter) | Enforced |
| Crash/hang/segfault takes down the host | **Enforced** (worker is a separate process) | Enforced |
| Host actions are mediated + audited (vault, LLM; broadcast when added) | **Enforced** (only the bridge is reachable) | Enforced |
| Credentials in the worker's own env | **Enforced** (allowlist env, Section 5.3) | Enforced |
| `import backend.*` | **Guardrail** (curated sys.path + meta-path blocker + packaging change, Section 5.4). A determined module can still read host source off disk and exec it | **Enforced** (host source not in the container) |
| Reading host secrets/DB **off disk** (`open("data/secrets.json")`) | **NOT enforced** same-uid. Becomes an overt, scanner-flagged act, not a sanctioned API | **Enforced** (no host files mounted; distinct uid) |
| Reading host/sibling **`/proc/<pid>/environ`** or memory (Linux same-uid) | **NOT enforced**. Same uid can read sibling and host environ unless procfs is hardened | **Enforced** (separate PID namespace; distinct uid) |
| Network egress outside declared `network.hosts` | **NOT enforced** (declared + scanned only) | **Enforceable** via per-module network policy / egress proxy (still optional) |
| CPU / memory / disk exhaustion (local DoS) | **NOT enforced** (rlimit best-effort on POSIX) | **Enforced** (container limits) |
| Arbitrary syscalls | **NOT enforced** | Hardenable (seccomp/cap-drop/read-only rootfs) |

**The v1 honest claim, verbatim for docs and UI:** "Community module backends run
in a separate process with no host credentials in their environment and no access
to host internals; every host action goes through an audited capability bridge.
This removes the sanctioned in-process pathway and isolates faults. It is not, by
itself, containment against a determined malicious module on the same OS user;
for that, run the optional container-per-module mode. Install only from sources
you trust." No "sandboxed and safe" badge.

If we ship v1 and let the README imply "isolated == contained," that is the
overclaim to reject in review.

## 4. Current state (analysis)

**Loader.** `backend/modules/__init__.py` `register_modules(app)` (called once at
import time from `backend/main.py:376`, before the static mount): walks built-ins,
then `data/modules/{id}/`, `importlib.import_module(child.name)` (community parent
dir is pushed onto `sys.path`), and `app.include_router(mod.router)`. The module
router executes in the host process, host event loop, host interpreter, with
unrestricted access. A failed import is recorded `status=failed`, not fatal.

**The reference consumer's real coupling surface.** `ageniusdesk-community-modules/
modules/youtube-research/` is the only community module and the forcing function.
A `grep` of its `backend.*`/host touchpoints (not the 3 the prior continuation
note listed; there are **six**, and the vault one is deeper than "import write"):

| # | Coupling | Code | Under isolation |
|---|---|---|---|
| 1 | Auth dependency | `router.py`: `Depends(require_trusted_request)` on routes | **Removed.** Host authenticates + CSRF-checks `/api/{id}/*` before proxying. Module router drops it. Net less module code. |
| 2 | Background jobs | `router.py`: `asyncio.create_task(_run_job)` / `_run_deepdive` | **Free.** The worker has its own event loop; `create_task` works in-worker. No bridge. |
| 3 | Live progress | `router.py`: `backend.websocket.manager.broadcast("youtube-research:job", ...)` | **Dropped in v1.** The iframe already polls `/api/{id}/jobs` through the proxy (`module.js`), so progress works with no host WS. The broadcast call is removed on re-port; a host->iframe WS relay + `broadcast` capability is future work (Section 5.5c). |
| 4 | Vault writes + reads | `artifacts.py`: imports `notes.storage`/`notes.index` AND does direct FS ops on `vault.VAULT_DIR` (`exists`, `read_text`, `iterdir`, `mkdir`, `rmdir`, `unlink`) for dedup, folder picker, move | **Biggest port.** Worker has no host vault access. Needs a notes bridge surface: write, read, list-folders, make-folder, move, delete, index-remove (Section 5.5a). |
| 5 | LLM keys | `llm.py`: `get_assistant_config()` + `decrypt_value()` + `PROVIDER_KEY_MAP` | **Bridge required.** Worker must never hold keys. Host runs the completion on the module's behalf (`assistant.complete`, Section 5.5b). |
| 6 | Job store | `store.py`: `backend.database.get_db`, table `youtube_research_jobs` | **Module-private SQLite** in the worker's own data dir. Removes a host coupling entirely; survives restart the same way (Section 5.6). |

`CONTRIBUTING.md` in the module repo currently says: "You MAY import from the
AgeniusDesk host (`backend.*`) since modules run in-process." Isolation deletes
that guarantee. That is the breaking change driving the version bump (Section 8).

### 4.1 Prerequisite (must fix first): module id is an unvalidated filesystem path

This is a pre-existing CRITICAL bug, independent of isolation but a hard blocker
for it, surfaced by review pass 1. `ModuleManifest.id` is an unconstrained `str`
(`module_registry.py:99`) and is used directly as a path component for
destructive operations:

- `installer.py:373` `final_dir = COMMUNITY_MODULES_DIR / manifest.id` then
  `:375` `shutil.rmtree(final_dir)` (reinstall overwrite),
- `installer.py:379` `shutil.move(module_root, final_dir)`,
- `installer.py:414` `target = COMMUNITY_MODULES_DIR / module_id` then `:418`
  `shutil.rmtree(target)` (uninstall).

An `id` of `..` resolves `COMMUNITY_MODULES_DIR / ".."` to `data/`, so an
uninstall (or a reinstall overwrite) recursively deletes the **entire data
directory** (DB, secret store, vault). `../something` escapes anywhere the process
can write. The tarball extraction is path-hardened, but the id-to-path mapping is
not.

Isolation makes this worse, not better: under this spec the id also keys the UDS
path (`data/run/mod-{id}.sock`), the per-module data dir
(`data/modules/{id}/_data/`), the bridge token map, and the forced broadcast
channel namespace (`community:{id}:`). An unvalidated id becomes socket-path
traversal, data-dir escape, and channel spoofing on top of the delete bug.

Required fix (gates everything else):

- Enforce a strict, cross-platform-safe slug at manifest parse time with a
  pydantic validator on `ModuleManifest.id`: `^[a-z0-9][a-z0-9_-]{0,63}$` (1-64
  chars). Dots are forbidden entirely, which removes both `..` traversal and the
  Windows trailing-dot alias (`a.` resolves to `a`, so two ids could target one
  dir). Reject Windows reserved device names (`con`, `prn`, `aux`, `nul`,
  `com0-9`, `lpt0-9`) on every platform for portable installs.
- At every write/move/delete/spawn entry point, resolve the target and re-check
  `target.resolve().relative_to(COMMUNITY_MODULES_DIR.resolve())` before acting;
  raise on escape. Same containment check for the run dir and data dir.
- Treat this as phase 0 (Section 13); LANDED as a standalone patch ahead of the
  isolation work (commit `2865246` + the Windows hardening from review pass 2).

## 5. Design

### 5.1 Process model: reverse-proxy to a per-module worker

Each community module runs in its own worker process that hosts the module's
**existing** FastAPI `router` (unchanged) via uvicorn. The host reverse-proxies
`/api/{id}/*` to that worker.

Why reverse-proxy over pipe-RPC: the module's router code is unchanged (the win),
FastAPI/Starlette semantics (streaming, deps, validation) are preserved for free,
and, critically, **auth simplifies**: the host gates `/api/{id}/*` at the proxy
(session/edge/token + CSRF for mutations) *before* forwarding, so the worker never
sees the host session cookie and the module drops its auth dependency.

Mechanics the implementation must get right (each is a review checkpoint):

- **Transport.** POSIX: a Unix domain socket per worker under
  `data/run/mod-{id}.sock` (file mode 600, host uid). Windows: a random loopback
  TCP port on `127.0.0.1` (AF_UNIX support in uvicorn on Windows is not reliable).
  Loopback TCP is itself an exposure (any local process can connect); mitigated by
  the proxy secret (5.2).
- **Worker mounts the router at its real prefix** (`/api/{id}/...`) so route paths
  and `url_for` are unchanged. Host forwards method, path, query, body, and a
  safe header subset.
- **Header hygiene.** Host strips `Cookie` and `Authorization` (worker never sees
  host identity), strips hop-by-hop headers, and ADDS `X-AGD-Proxy-Secret`
  (5.2). Host MAY forward a minimal `X-AGD-User` (role only) if a module needs
  coarse role awareness; default is to forward nothing identifying.
- **Streaming + WebSockets.** Proxy must stream request and response bodies
  (no full buffering; youtube-research writes large transcripts). If a module
  declares its own WebSocket route, the proxy must support the upgrade; v1 MAY
  defer module-owned WS and document it. youtube-research needs no WebSocket at
  all in v1: its iframe polls `/api/{id}/jobs` through the proxy. The host->iframe
  WS relay (the `broadcast` capability) is deferred (5.5c).
- **Limits.** Per-request timeout, max body size, and max concurrent in-flight
  requests per worker, all configurable, all enforced host-side at the proxy.
- **Built-in modules stay in-process.** Isolation applies to `source=community`
  only. Built-ins are first-party.

### 5.2 The internal proxy secret (stop direct worker access)

A worker bound to loopback TCP (Windows) is reachable by any local process, which
would bypass host auth entirely. The host mints a random per-spawn
`proxy_secret`, injects it into the worker (allowlisted env), and the worker
**rejects any request lacking `X-AGD-Proxy-Secret == proxy_secret`** via ASGI
middleware installed by the worker bootstrap (not the module). UDS with mode 600
already constrains this on POSIX; the secret is defense-in-depth there and the
primary control on Windows.

### 5.3 Environment scrubbing (allowlist, never denylist)

The worker is spawned with an **allowlist** environment, not the inherited
`os.environ` minus a blocklist (a blocklist silently leaks the next secret
someone adds). The worker gets only:

- `PATH`, `LANG`/`LC_*`, `TMPDIR`/`TEMP`, `PYTHONHASHSEED` (minimal runtime).
- `PYTHONPATH` set explicitly by the host (5.4).
- The injected `AGD_MODULE_ID`, `AGD_BRIDGE_URL`, `AGD_BRIDGE_TOKEN`,
  `AGD_PROXY_SECRET`, `AGD_MODULE_DATA_DIR`.
- Any keys the module **declared** in `capabilities.env` AND that are non-secret
  (a declared env key that collides with a known secret name is refused).

Explicitly absent: `SECRET_KEY`, every `*_KEY`/`*_TOKEN`, `QDRANT_*`,
`DASHBOARD_MCP_TOKEN`, `AGD_ADMIN_TOKEN`, `AGD_WEBHOOK_TOKEN`, DB paths, edge-auth
config.

Caveat (already in the matrix): on Linux same-uid this protects the worker's own
env only; a determined worker can read the host's `/proc/1/environ`. The
container tier closes that.

### 5.4 Import isolation and the site-packages gotcha

We want `import httpx` / `yt-dlp` / `anthropic` to work (the worker shares the
interpreter's third-party deps) but `import backend.*` to fail.

**The trap:** the Dockerfile runs `pip install '.[assistant]'` and
`pyproject.toml` has `packages.find include = ["backend*"]`, so **`backend` is
installed into site-packages**, not only present at the repo root. Excluding the
repo root from the worker's `sys.path` therefore does NOT block `import backend`.
A naive sys.path approach is a false sense of security; an adversarial reviewer
would (correctly) walk right through it.

v1 takes three layered steps, all guardrail-grade, honest about it:

1. **Packaging change:** stop shipping `backend` into site-packages. The image
   installs dependencies only and runs the host from the `/app` source tree
   (host already runs with cwd `/app`). Then `backend` is importable only via the
   source root.
2. **Curated `sys.path`:** the worker's `PYTHONPATH` contains the module dir and
   site-packages, NOT `/app`. After step 1, `import backend` is now a
   `ModuleNotFoundError`.
3. **Meta-path blocker:** the worker bootstrap installs a `sys.meta_path` finder
   that raises `ImportError` on `backend` / `backend.*`. Belt and suspenders;
   removable by a determined module, hence guardrail not boundary.

**Bootstrap location (review pass 1).** The worker entrypoint must live OUTSIDE
the `backend` package, e.g. a top-level `agd_module_worker/` package (or a
standalone `worker.py` launched by absolute path), so that starting a worker does
not itself `import backend`. A bootstrap under `backend/modules/_runtime/` would
import the host package at process start and defeat the ban before the module
even loads. The bootstrap's own import graph is stdlib + third-party + the module
only.

A hard guarantee (adversary-3) needs the container tier (host source not present)
or a fully separate worker venv (heavier: re-install third-party deps per
worker). v1 chooses the lighter guardrail and says so.

### 5.5 The host-API bridge (`/api/_host/*`)

A dedicated bridge server bound to **loopback only** (`127.0.0.1`, separate from
the public `0.0.0.0:3000` listener so `_host` is never exposed on the public
bind). Authed by a per-module bearer token (`AGD_BRIDGE_TOKEN`, random per spawn,
in host memory mapping token to module id and its consented capabilities).
Requests carrying a browser session cookie are rejected (not a browser surface).

Dispatch is a whitelist gated by the module's declared + consented capabilities.
Two namespaces are needed by the reference consumer (`notes.*`,
`assistant.complete`); a third (`broadcast`) is specified but optional/future
(5.5c):

**a) `notes.*`** (gated by `capabilities.filesystem.write_paths`, and a new
`read_paths`, Section 6):

| Method | Args | Capability check |
|---|---|---|
| `notes.write` | `path`, `content` | `path` resolves under vault AND under a declared write path (reuse `storage.resolve()` traversal guard; reject `..`, absolute, backslash) |
| `notes.read` | `path` | under a declared read path |
| `notes.list_folders` | `rel` | under a declared read path |
| `notes.make_folder` | `rel` | under a declared write path |
| `notes.move` | `from`, `to` | both under declared write paths |
| `notes.delete` / `notes.index_remove` | `path` | under a declared write path |

The bridge calls the existing `backend.modules.notes.storage`/`index` host-side.
Path validation is enforced **server-side in the bridge, never trusted from the
worker**, and needs TWO validators (review pass 1): `storage.resolve()` is
note-oriented (it appends `.md` and is file-shaped, `storage.py:93`), which covers
`notes.write/read/index_remove`, but the directory operations (`list_folders`,
`make_folder`, `move`) need a **directory-safe** validator that canonicalizes and
confirms containment without forcing a `.md` suffix. Both validators resolve
symlinks first, then check `relative_to` the vault root AND the declared
`read_paths`/`write_paths` subtree, in that order, after canonicalization.

**b) `assistant.complete`** (gated by a new `host.assistant` capability):

```
assistant.complete { system, user, model?, max_tokens? } -> { text }
```

The host resolves the provider/key from the saved assistant config and runs the
completion through a **dedicated tool-free executor, NOT the assistant's
`_dispatch_chat`** (review pass 1). `_dispatch_chat` (`providers.py:460`) routes to
per-provider chat functions that inject MCP tools, including mutating ones
(`tools.py` `trigger_workflow`, `import_workflow`); reusing it would hand a
community module the assistant's full tool surface through a "completion" call.
The bridge executor MUST be tool-free by construction: no MCP fetch, no `tools`
key in the provider payload, ever. It keeps the large output budget and the
host-owned max_tokens clamp/retry that `llm.py` does today. Shared provider code
may be reused only behind a hard, non-overridable tools-disabled path (not a
caller-supplied flag). **The key never crosses into the worker.** This is the
cleanest answer to coupling #5 and is strictly better than "hand the worker a
scoped token," which would put a usable credential in the sandbox. The module
supplies only prompt + a model name within the operator's configured provider; it
cannot set the provider base URL (a `custom_base_url` provider is operator-set,
see Section 11.8). Cost note: a module can spend the operator's LLM budget via
this call (they consented to the module); an optional per-module budget cap is a
follow-up, not a v1 gate.

**c) `broadcast`** (OPTIONAL, future; gated by a new `host.broadcast` capability):

```
broadcast { event, payload } -> { ok }
```

**Not needed by the reference consumer and not wired end to end in v1** (review
pass 1). youtube-research's frontend already polls `/api/{id}/jobs` through the
proxy (`module.js`), so live progress works under isolation with no broadcast at
all. The host->iframe WS relay does NOT exist today: the v0.3 frontend bridge
whitelist is `fetch`/`notify`/`navigate`/`openInHarness` only
(`community-modules.js:77-87`), with no WS method. Shipping `broadcast` therefore
requires its own phase: add a host->iframe WS relay method to the frontend
bridge. When built, the host MUST force the channel namespace to the module id
(emitted channel `community:{id}:{event}`; reject any cross-namespace `event`) to
stop a module spoofing host or sibling channels, and cap payload size. Until
then, modules use polling through the proxy.

Anything not in the whitelist is denied. No generic "run host code" method.

### 5.6 Module-private storage

Replace the host-DB coupling (#6) with a per-module data dir
`data/modules/{id}/_data/` (bind-available to the worker; the only path it may
write outside the bridge). youtube-research moves `youtube_research_jobs` into a
private `jobs.db` (SQLite) there. Survives restart and uninstall-preserves /
uninstall-purges per the existing module data lifecycle. No host DB handle, no
shared schema, no name collisions with host tables.

### 5.7 Lifecycle

- **Spawn:** on `register_modules`, for each compatible community module, spawn a
  worker (eager) or on first request (lazy). Eager is simpler to reason about and
  surfaces failures at boot; lazy saves memory with many modules. v1: eager, with
  a `lazy` flag reserved.
- **Health:** worker exposes `/_worker/health`; host probes before marking
  `status=loaded`. Probe failure -> `status=failed`, host stays up (parity with
  today's import-failure behavior).
- **Restart:** on crash, host restarts with capped exponential backoff and a
  circuit breaker (N failures in a window -> `status=failed`, stop hammering).
- **Uninstall / disable:** host SIGTERMs the worker, removes the proxy route,
  revokes the token, optionally purges `_data/`.
- **Orphans:** host records worker PIDs; on boot it kills any stale workers from a
  previous run before respawning. UDS files are unlinked on stop and on boot.
- The existing `POST /api/admin/restart` (SIGTERM self under `restart:
  unless-stopped`) still works and now also brings workers back.

### 5.8 The container tier (the real boundary, layered later under the same bridge)

The product already centers on Docker and the recommended deploy mounts the
socket. The strong confinement option reuses **the entire bridge unchanged** and
swaps the worker from a subprocess to a container per module:

- Image: the same app image (or a slim runtime), entrypoint = the worker
  bootstrap. Module code + its `_data/` bind-mounted read-appropriately; **no host
  secrets, no host source, no `data/` beyond the module's own dir mounted.**
- `--user` distinct uid, `--read-only` rootfs + tmpfs, `--cap-drop ALL`, optional
  seccomp; memory/CPU limits; a dedicated docker network with egress policy (or an
  egress proxy enforcing `network.hosts`).
- Bridge reachable at the host gateway IP over the per-module token; proxy reaches
  the worker over the container's published loopback port / shared internal net.

This closes every "NOT enforced" row in Section 3 (off-disk secret reads, /proc,
DoS, syscalls, network). It depends on the Docker socket and adds per-module
container overhead. **Recommendation:** ship the subprocess tier + bridge first
(defines and proves the contract, re-ports youtube-research once), then add the
container tier as an operator-selectable confinement mode. Module code does not
change between tiers.

## 6. Capability model changes

Extend `Capabilities` (in `backend/module_registry.py`):

- `filesystem.read_paths: list[str]` (new) - vault subtrees the module may read
  via the bridge. youtube-research declares its research subtree.
- `host: { assistant: bool, broadcast: bool }` (new) - which bridge namespaces
  beyond notes the module may call. Declared, consented, and enforced by the
  bridge dispatcher. The scanner cross-checks: a module that calls
  `assistant.complete` without declaring `host.assistant` is an undeclared-
  capability HIGH finding (same machinery as the network/fs diff today).
- `isolation: "subprocess" | "container" | "in_process"` resolution is an
  **operator/host** decision, not a module self-declaration (a module cannot ask
  to run in-process). Manifest may carry `min_app_version` to require the bridge.

Scanner additions: detect bridge misuse, and (transition aid) downgrade
`backend.*` host imports from INFO to a HIGH "will not run under isolation"
finding once the contract flips.

## 7. Re-porting youtube-research (the reference)

Port once, against the bridge. Concrete diff:

- `router.py`: drop `Depends(require_trusted_request)` (host gates at the proxy).
  Remove the `manager.broadcast(...)` call entirely; the iframe already polls
  `/api/{id}/jobs` through the proxy, so live progress works with no host WS in
  v1 (the `broadcast` capability is deferred, 5.5c). `asyncio.create_task`
  unchanged (own loop).
- `llm.py`: delete `get_assistant_config`/`decrypt_value`/`PROVIDER_KEY_MAP` and
  the provider dispatch; replace `complete()` with a single
  `bridge.assistant.complete(system, user, model, max_tokens)`. The max_tokens
  retry/clamp moves host-side. Net: `llm.py` shrinks to a thin bridge call.
- `artifacts.py`: replace every direct `vault.VAULT_DIR` FS op and the
  `notes.storage`/`index` imports with `bridge.notes.*` calls. Path math
  (slug/dedupe/dest) stays in the module; the privileged FS action is the bridge's.
- `store.py`: swap `backend.database.get_db` for a private SQLite in
  `AGD_MODULE_DATA_DIR/jobs.db`. Schema unchanged.
- A small `bridge.py` client in the module (httpx to `AGD_BRIDGE_URL` with the
  token). This becomes the documented module SDK pattern.
- `CONTRIBUTING.md`: "no `backend.*` imports"; document the bridge SDK; bump
  `min_app_version`.

**Data migration.** Vault artifacts already live in `data/workspace/research/`
and are written via the bridge to the same tree, so they are preserved untouched.
Job history in the host `youtube_research_jobs` table would otherwise be orphaned;
provide a one-time importer on first sandboxed boot (copy host rows -> private
`jobs.db`) OR accept history reset (artifacts persist; the list rebuilds going
forward). Recommend the importer for a clean upgrade; it is ~20 lines.

## 8. Versioning and transition

Breaking the `backend.*` contract is a major change. Recommended path:

- **0.3.0 (next minor, currently unreleased; the iframe sandbox also lands here):
  dual-mode, opt-in.** Ship the subprocess runner + bridge. Per-module operator
  flag `isolation: subprocess | in_process`. Existing installs keep working
  in-process (status quo); new installs default to subprocess. Re-ported
  youtube-research runs sandboxed and is the reference. (Current released version
  is 0.2.0.)
- **v0.4: hard cutover.** Community modules run isolated only; `in_process` for
  community sources is removed. Built-ins remain in-process (first-party). The
  container tier ships here or shortly after as the operator-selectable boundary.

Dual-mode costs a maintained in-process path for one minor version, but de-risks
the rollout for any third party who installed against the old contract. With
exactly one first-party community module today, the blast radius is small either
way; the deciding factor is whether we expect third-party installs before v0.4.

## 9. Cross-platform reality

- **Linux (the deploy target, in Docker):** UDS, rlimit, and the container tier
  all available. The real boundary is reachable here.
- **macOS (dev):** UDS works; rlimit partial; container tier via Docker Desktop.
- **Windows (operator dev boxes):** loopback TCP + proxy secret (UDS unreliable in
  the stack); no rlimit; container tier only under Docker. The subprocess tier on
  bare Windows is the weakest; document it as "guardrail, not boundary; for real
  isolation run in Docker."

## 10. Performance and overhead

- Per-request: one extra loopback hop (sub-millisecond). Streaming preserved.
- Per-module: one Python interpreter resident (~30-50 MB) for the subprocess
  tier; a container (~tens of MB + image) for the container tier. With a handful
  of modules this is negligible; document the per-module cost so an operator with
  many modules can choose lazy spawn.
- Bridge calls add a loopback round-trip per host action (vault write, LLM call);
  LLM latency dominates, vault writes are local. Acceptable.

## 11. Attack surface and known weaknesses (read this adversarially)

Explicit invitations to break it. Each is either mitigated or accepted-and-stated.

0. **Module-id path traversal (pre-existing, CRITICAL).** Unvalidated `id` used in
   `rmtree`/`move`/socket/data-dir paths; `id == ".."` deletes `data/`. **Must be
   fixed first** (Section 4.1); phase 0 gates the rest.
1. **Same-uid `/proc` and on-disk reads (Linux).** Adversary-3 reads host/sibling
   `environ` and `data/secrets.json` directly. **Accepted in v1; closed by the
   container tier.** Do not let docs imply otherwise.
2. **Import blocker is removable.** A determined module deletes the meta-path
   finder or reads host source off disk and `exec`s it. **Guardrail by design;
   real fix is the container tier (no host source present).**
3. **Proxy-secret / bridge-token theft via `/proc` or memory (same-uid).** Same
   root cause as #1. Container tier (PID namespace) closes it. Tokens are
   per-spawn and loopback-scoped to limit value off-box.
4. **Direct worker hit bypassing host auth.** Mitigated by UDS mode-600 (POSIX)
   and the mandatory `X-AGD-Proxy-Secret` (all platforms). Review the middleware
   ordering so the secret check runs before any module routing.
5. **`_host` bridge exposed on the public bind.** Mitigated by binding the bridge
   to loopback only and rejecting cookie-bearing requests. Verify it is a separate
   listener, not a path on `0.0.0.0:3000`.
6. **Path traversal through the notes bridge.** Mitigated by reusing
   `storage.resolve()` and enforcing declared-path scoping server-side; never
   trust the worker's path. Add traversal tests (`..`, absolute, backslash,
   symlink target).
7. **Channel spoofing via broadcast.** Mitigated by forced `community:{id}:`
   namespace. Only relevant once `broadcast` is built (5.5c); not in v1.
8. **SSRF via `assistant.complete` model/endpoint.** The host controls the
   provider URL from saved config; the module supplies only prompt + model name
   within the configured provider. A `custom_base_url` provider is operator-set,
   not module-set. Confirm the module cannot inject an arbitrary base URL.
9. **LLM cost abuse.** Accepted (consented install); optional per-module budget
   cap is a follow-up.
10. **Crash-loop DoS on the host.** Mitigated by backoff + circuit breaker.
11. **Orphan workers / port/socket leakage across restarts.** Mitigated by PID
    tracking + boot cleanup + UDS unlink.
12. **Env allowlist regression.** A future code change reverts to passing
    `os.environ`. Mitigated by a test asserting the worker env contains none of a
    known secret-name set.
13. **Worker shares the host venv (v1).** It can import any installed third-party
    package (intended) but that includes anything with its own dangerous surface.
    Accepted; container tier or per-module venv narrows it later.

## 12. Data, schema, API, config changes

- `Capabilities`: add `filesystem.read_paths`, `host.{assistant,broadcast}`.
- `RegistryEntry`: carry `isolation` mode + worker state (pid/health) for the UI.
- New host bridge app + dispatcher + per-module token store (in-memory).
- New worker bootstrap entrypoint as a top-level package OUTSIDE `backend` (e.g.
  `agd_module_worker/`), launched by path so process start does not import
  `backend` (Section 5.4). The loader (in `backend`, e.g. `backend/modules/
  _runtime/`) spawns and reverse-proxies instead of import+mount for community
  sources.
- Strict `ModuleManifest.id` validator + containment re-checks at every
  write/move/delete/spawn entry point (Section 4.1).
- Packaging: stop installing `backend` into site-packages (Dockerfile/pyproject).
- Config: `AGD_MODULE_ISOLATION` default, spawn mode, timeouts, body/limit knobs.
- `static_router.py` unchanged (frontend assets still host-served; iframe intact).
- Module repo: `bridge.py` SDK, ported youtube-research, `CONTRIBUTING.md`,
  manifest `read_paths`/`host` + `min_app_version`.
- Docs: `security.md` (flip the "Community modules: backend accepted" accepted-risk
  row to the two-tier enforcement matrix), `modules.md`, `overview.md` (loader
  now spawns/proxies), CHANGELOG, ROADMAP.

## 13. Implementation phases

0. **Prerequisite:** strict `ModuleManifest.id` validator + containment re-checks
   at all write/move/delete/spawn paths (Section 4.1). Landable as a standalone
   patch ahead of the rest.
1. Worker bootstrap, as a top-level package outside `backend` (env scrub,
   sys.path/meta-path blocker, proxy-secret middleware, health) + packaging
   change. Prove `import backend` fails, host secrets absent from env, and the
   bootstrap itself does not import `backend`.
2. Reverse proxy in the loader (UDS/loopback, header hygiene, streaming, limits)
   + lifecycle (spawn/health/restart/orphan cleanup). Run an unmodified trivial
   module through it.
3. Host bridge (loopback listener, token store, dispatcher) + `notes.*` namespace
   with server-side path scoping.
4. `assistant.complete` namespace (tool-free executor).
5. Capability model + scanner additions (read_paths, host.*, host-import HIGH).
6. Re-port youtube-research onto the bridge + private store + data importer
   (polling for progress; no broadcast).
7. Dual-mode flag, docs (honest matrix), CHANGELOG/ROADMAP, tests.
8. (Later) `broadcast` namespace + the host->iframe WS relay (5.5c), then the
   container tier under the same bridge.

## 14. Testing

- Module id: manifest with `id` of `..`, `../x`, absolute, dot-leading, or
  >64 chars is rejected at parse; uninstall/reinstall with a crafted id cannot
  resolve outside `COMMUNITY_MODULES_DIR` (no `data/` delete). Phase 0 gate.
- Worker env: asserts none of `{SECRET_KEY, *_KEY, *_TOKEN, QDRANT_*}` present;
  `import backend` raises; third-party import works; the bootstrap module's
  import does not pull in `backend`.
- Proxy: auth enforced host-side (unauth `/api/{id}/*` blocked before forward);
  CSRF on mutations; direct worker hit without `X-AGD-Proxy-Secret` rejected;
  streaming a large body round-trips; timeout + body-size limits fire.
- Bridge: token required; cookie-bearing request rejected; `notes.write` (note
  validator) and `notes.make_folder`/`move` (dir validator) reject traversal +
  out-of-declared-scope (`..`, absolute, backslash, symlink target);
  `assistant.complete` is tool-free (no `tools` in the provider payload) and
  cannot set the base URL; unknown method denied. (`broadcast` namespace tests
  land with that phase, not v1.)
- Lifecycle: crash -> restart with backoff; circuit breaker trips; uninstall
  stops worker + revokes token; orphan killed on boot.
- youtube-research end-to-end under isolation on 3066: discover -> inspect ->
  consent -> install -> restart -> run -> captions -> breakdown (via bridge LLM)
  -> classify -> vault write (via bridge) -> live progress (via iframe polling
  through the proxy) -> persist (private DB) -> View in Harness. Zero `backend.*`
  imports remain.
- Data importer: host `youtube_research_jobs` rows -> private `jobs.db`.
- `uv run pytest`; lint touched files `uvx ruff check` (line-length 120).

## 15. Open questions / decisions on record

1. **Sandbox depth for v1:** subprocess + bridge (this spec's recommendation) vs
   wait and ship container-per-module directly. Recommendation: subprocess first
   to define and prove the bridge, container tier under the same contract next.
2. **Transport:** reverse-proxy (recommended; router unchanged, auth simplifies)
   vs pipe-RPC.
3. **Transition:** dual-mode opt-in in v0.3 then hard cutover v0.4 (recommended)
   vs straight v0.4 cutover. Hinges on whether third-party installs are expected
   before v0.4.
4. **Re-port youtube-research now** (recommended; it validates the bridge surface)
   vs keep it in-process under a trusted flag during transition.
5. Eager vs lazy worker spawn default (recommend eager; lazy reserved).
6. Job-history migration: ship the importer (recommend) vs accept reset.
7. Per-module LLM budget cap: in v1 or follow-up (recommend follow-up).
8. Network egress enforcement: stays declared-only in the subprocess tier; an
   egress proxy is a container-tier add. Confirm we are honest that v1 does not
   enforce it.
