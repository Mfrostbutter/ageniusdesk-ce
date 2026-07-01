# AgeniusDesk CE — Full Code & Security Review

Date: 2026-07-01
Branch: `feat/fleet-health-contribution-api`
Scope: entire repository (~28k lines Python, ~20k lines JS). Reviewed the auth
core by hand and fanned out five focused reviewers across docker_mgr, the
n8n proxy/webhook surface, the assistant/agent-fleet/MCP/knowledge surface, the
frontend, and the core app plumbing. Every High/Critical below was re-verified
against source before inclusion.

> **Update 2026-07-01 — all four High findings (#1–#4) are FIXED and covered by
> regression tests (`tests/test_high_severity_fixes.py`, `tests/test_assistant_authz_ssrf.py`).**
> The Medium/Low findings below remain open.
>
> - **#1** Agent Fleet router raised to `require_role("admin")`
>   (`agent_fleet/router.py:39`). The read paths also import/execute vault code,
>   so the whole surface is admin-gated.
> - **#2** `/api/mcp` router raised to `require_role("operator")`
>   (`assistant/mcp_router.py`); every MCP server-side fetch now runs the URL
>   through `assert_safe_probe_url` in `_normalize_mcp_urls`
>   (`assistant/mcp_client.py`), blocking metadata/link-local/reserved targets.
> - **#3** Community-template substitution now runs on the parsed JSON object's
>   string leaves via `_apply_subs` (`docker_mgr/templates.py`), so a field value
>   can never inject structure (Privileged / host binds).
> - **#4** The three hand-rolled markdown renderers now `esc()` before their
>   inline transforms and drop non-http(s) link hrefs (`assistant.js`,
>   `errors.js`, `codelab.js`); the Agent Fleet view sanitizes `marked` output
>   with DOMPurify (`agent-fleet.js`), failing safe to escaped text if the
>   sanitizer can't load. Note #11 (notes snippet) is a related Medium still open.

## Executive summary

The codebase is **well-hardened for its class**. The authentication core is
strong: secure-by-default login gate, PBKDF2-SHA256 @ 600k iterations, DB-backed
sessions that store only a token hash, Fernet secret-at-rest, constant-time
compares in the password/token/TOTP paths, single-use hashed reset tokens,
double-submit CSRF layered on `SameSite=Strict` cookies, and fully parameterized
SQL throughout. Path traversal is consistently guarded (notes vault, themes,
static JS, module assets). Secrets are never returned to the browser in
plaintext. The prior hardening pass (`docs/code-review/security-hardening-*`)
closed the spoofable-edge-auth and open-API-route issues.

The remaining risk is **not in the crypto or the SQL — it is in the
authorization model and in a few server-side-fetch / template surfaces.** One
theme dominates and is the highest-leverage fix:

> **The `require_role` floor is applied inconsistently.** Several of the most
> dangerous surfaces (agent code execution, MCP server management, the dashboard
> MCP tools, the music player) sit behind only "any authenticated identity" or
> no router-level dependency at all. The `viewer` role is meant to be read-only,
> but it currently reaches in-process code execution, server-side request
> forgery, and secret-name enumeration. Fixing the role floors closes four of
> the findings below at once.

Deployment caveat that bounds all of this: the project documents that the
console must not be exposed without auth and must not mount a Docker socket
unless fully trusted. With `AGD_DISABLE_LOGIN=true` (open install) the global
gate is a no-op and every finding below becomes reachable unauthenticated — that
mode is for trusted localhost only and is logged loudly at startup.

## Findings by severity

| # | Severity | Finding | Location |
|---|----------|---------|----------|
| 1 | High | Any authenticated identity (viewer) can author + execute arbitrary Python in-process | `agent_fleet/router.py:42`, `vault_agents.py:80` |
| 2 | High | MCP server management is SSRF with no role floor; responses reflected | `assistant/mcp_router.py:12`, `mcp_client.py:176-306` |
| 3 | High | Community-template field substitution is JSON injection → privileged/host-mount container → host root | `docker_mgr/templates.py:526-529, 580-583` |
| 4 | High | DOM XSS in four hand-rolled markdown renderers fed by LLM/agent output | `assistant.js:869`, `errors.js:270`, `codelab.js:1540`, `agent-fleet.js:23` |
| 5 | Medium | `inspect` returns cleartext env (secrets) for any container; lifecycle actions hit any container, not just AGD-managed | `docker_mgr/router.py:230, 322, 490` |
| 6 | Medium | Per-instance secret scope bypassable by repointing an in-scope instance URL to attacker host | `n8n_credentials/router.py:201-237`, `config.py:427` |
| 7 | Medium | Operator-controlled instance URL is an unrestricted SSRF egress + blind host/port oracle | `n8n_proxy/client.py:257-367`, `router.py:253` |
| 8 | Medium | Dashboard MCP endpoint: no role floor + `write_note`/`append_note` + secret-name enumeration; misleading auth comment | `dashboard_mcp/server.py:136-263, 373` |
| 9 | Medium | Community-template `container_config` HostConfig is unvalidated (Privileged/Binds/host namespaces allowed) | `docker_mgr/templates.py:492-608` |
| 10 | Medium | Assistant prompt-injection → state-changing tools (trigger/activate/import/workspace_write) with no confirmation | `assistant/tools.py:184-273`, `workspace_tools.py` |
| 11 | Medium | Notes search snippet rendered unescaped (stored XSS in multi-user install) | `notes.js:205` |
| 12 | Low | OTLP trace ingest is open when the receiver is enabled without a token | `main.py:234-242` |
| 13 | Low | Public API key compared with `==` not `hmac.compare_digest` (negligible; compares hashes) | `public_api/api_keys.py:70-75` |
| 14 | Low | `/api/music/*` has no role granularity; trigger token readable by any viewer | `player/music_router.py:171, 406` |
| 15 | Low | `AGD_TLS_VERIFY` not honored in the n8n_credentials module (inconsistent, fail-secure) | `n8n_credentials/router.py:186, 314` |
| 16 | Low | Community-module static assets served pre-auth (frontend assets only) | `modules/static_router.py:39` |
| 17 | Low | TOTP has no intra-window replay lockout | `totp.py:55` |
| 18 | Low | Path-segment IDs not URL-encoded into the n8n API (negligible; same-key caller) | `n8n_proxy/client.py:509, 565` |
| 19 | Low | n8n encryption keys / minted bundle secrets written plaintext to `data/template_state/*.json` (chmod 600) | `docker_mgr/deployer.py:55-148`, `template_state.py:51` |

---

## High

### 1. Any authenticated identity can execute arbitrary Python in-process

`backend/modules/agent_fleet/router.py:42` gates the whole router with
`Depends(require_trusted_request)`, which only asserts that *some* identity
exists — it enforces no role. `POST /api/agent-fleet/agents` (`register_agent`)
accepts an arbitrary Python `code` blob, syntax-checks it, and writes it to
`data/workspace/agents/<id>/graph.py`. On a run, `vault_agents._load_module`
does `spec.loader.exec_module(module)` (`vault_agents.py:80`) and calls its
`build()` — the operator-supplied module is imported and executed **in-process
with full host privileges**: it can read `data/secrets.json`, the Fernet key,
env, and the mounted Docker socket.

Two things make this a finding rather than by-design:
- **Wrong gate.** A `viewer`-role session can register and trigger code. The
  assistant and knowledge routers correctly use `require_role("operator")`; this
  one does not.
- **No isolation.** The community-module system has an AST scanner
  (`modules/scanner.py`) and subprocess/container sandboxing (`_runtime/`). Vault
  agents bypass all of it.

Exploit: any authenticated user registers a `graph.py` whose `build()` reads and
exfiltrates the decrypted secret store or opens a reverse shell. (Running it
needs the optional `langgraph` extra installed; registration always succeeds.)

Fix: raise the router to `require_role("admin")`, and run vault agents under the
existing subprocess/container isolation + scanner.

### 2. MCP server management is unrestricted SSRF with no role floor

`backend/modules/assistant/mcp_router.py:12` mounts `/api/mcp` with **no
router-level role dependency**; only the three `n8n-mcp/*` subroutes carry
`require_role("operator")`. So `POST /servers`, `PUT /servers/{id}`,
`POST /servers/{id}/test`, and `GET /servers/{id}/tools` are reachable by any
authenticated identity including **viewer**. Each performs a server-side
`httpx` request to a fully caller-controlled `url` (`mcp_client.py:176-306`), and
there is **no** internal-range guard (the SSRF guard `assert_safe_probe_url`
exists but is applied only to the Ollama probe in `providers.py`).

A caller points the URL at `http://169.254.169.254/…` (cloud metadata), an
internal admin panel, or a `localhost` service. Impact is amplified because
responses are reflected: `discover_tools` surfaces parsed tool data via
`GET /servers/{id}/tools`, and `execute_tool` returns `resp.text[:2000]` —
turning blind SSRF into a read primitive against the internal network. The same
unguarded-fetch pattern exists (gated to `operator`, lower reach) in
`knowledge/backends.py:99` and `rag.py:34`.

Fix: add `Depends(require_role("operator"))` to the `/api/mcp` router and route
every operator-supplied fetch URL (MCP, Qdrant/knowledge, RAG) through a shared
allowlist that blocks link-local / metadata / RFC1918 / loopback ranges.

### 3. Community-template field substitution is JSON injection → host root

`backend/modules/docker_mgr/templates.py:526-529` (single) and `:580-583`
(bundle) build a container config by string-substituting operator-supplied field
values into **serialized JSON** and re-parsing it:

```python
config_str = json.dumps(template_def.get("container_config", {}))
for key, val in subs.items():
    config_str = config_str.replace(f"{{{key}}}", val)   # val = str(v), NOT JSON-escaped
config = json.loads(config_str)
```

`subs` includes `**{k: str(v) for k, v in f.items()}` where `f` is the deploy
request's `fields` dict — **unbounded and fully operator-controlled**
(`router.py:533`, only `port` is validated). A value containing `"` breaks out
of its string and injects arbitrary JSON. Given any community template with a
placeholder (the whole point of them), an operator supplies a field value like:

```
"x\"],\"HostConfig\":{\"Privileged\":true,\"Binds\":[\"/:/host:rw\"]},\"z\":[\""
```

After substitution + `json.loads`, the config gains `Privileged: true` and a
bind of host `/`, passed straight to `docker.containers.create()` via the mounted
socket → **host root**. Community templates load from disk
(`/app/data/templates/`), not HTTP, so an HTTP-only operator can't author one
directly — which is exactly why this injection is a real escalation from
operator to host root rather than a redundant capability.

Fix: substitute into the parsed object (walk the dict, replace leaf strings
only) or `json.dumps`-escape each value before insertion; see #9 for the
HostConfig allowlist that should back it up.

### 4. DOM XSS in the hand-rolled markdown renderers

The app has four hand-rolled markdown renderers; three apply regex transforms to
text that was never HTML-escaped, then assign to `innerHTML`. Because
assistant/agent output flows through them, a prompt-injected LLM reply, poisoned
MCP/tool output, or RAG-sourced content containing `<img src=x onerror=…>`
executes in the operator's session.

- `assistant.js:869` `renderMarkdown` — fenced code is escaped (line 872) but
  `inline()` and paragraph/heading/list text are not; verified `<`/`>` pass
  through untouched. The link rule also accepts `javascript:` URIs.
- `errors.js:270` `window.__askErrorAI` — LLM triage response transformed with
  newline/bold regex and injected unescaped. Reachable from every error item.
- `codelab.js:1540` `renderMd`/`il()` — same inline-not-escaped pattern.
- `agent-fleet.js:23` — `marked.parse()` with no DOMPurify; `marked` passes raw
  HTML through by design. Renders `run.proposal_md` / `run.triage_md`.

The correct pattern already exists in `assistant-dock.js:82` (`fmtMarkdown` calls
`esc()` first), proving it was known.

Fix: route all markdown through one helper that escapes HTML first (or
`marked` + DOMPurify with a URL-scheme allowlist that blocks `javascript:`).

---

## Medium

### 5. `inspect` leaks container env secrets; lifecycle actions hit any container

`docker_mgr/router.py:230` `inspect` returns the full `docker inspect` payload —
including `Config.Env` in cleartext — for **any** `container_id`, with no check
that the target carries the `ageniusdesk.managed` label. Every deployed
container's `N8N_ENCRYPTION_KEY`, `POSTGRES_PASSWORD`, etc. is readable by any
operator. `destroy` (`:322`) and `container_action` (`:490`) likewise operate on
any container on the shared host (only `_guard_not_self` protects the dashboard's
own container). Fix: restrict to labelled/managed containers and redact secret
env in the inspect response, or document that operators are full host Docker
admins.

### 6. Per-instance secret scope is bypassable by repointing the instance URL

The mirror gate `is_secret_allowed_on_instance(secret, instance_id)`
(`n8n_credentials/router.py:201`) checks scope against the instance **id** only.
But `PUT /api/n8n/instances/{id}` lets an operator change that instance's `url`
to any host. Flow: secret `S` scoped to instance `A` → edit `A`'s URL to
`http://attacker.example` → `POST /mirror` passes the id-only scope check → the
decrypted plaintext of `S` is POSTed to the attacker (`router.py:233`). The
`config.py:427` docstring already concedes this gate is not a general boundary;
this makes the "Applies To" scope UX-only. Direct cross-instance mirror to a new
attacker instance *is* correctly blocked. Fix: re-validate scope on URL change,
or treat scope as a hint and warn.

### 7. Instance URL is an unrestricted SSRF egress and blind oracle

Every outbound n8n call is built from a user-supplied `url` with a fixed path and
no internal-range filter (`client.py:257-367`). `POST /api/n8n/test-creds`
(`router.py:253`) returns distinct `error_class` values (`dns`/`auth`/`notfound`/
`timeout`), a blind SSRF oracle for mapping internal hosts/ports. The fixed path
limits metadata *data* exfil, but reachability probing plus the #6 exfil are
real. Fix: block RFC1918/loopback/link-local/metadata for instance hosts (opt-in
per install), and don't hand granular error classes to low-privilege callers.

### 8. Dashboard MCP endpoint has no role floor and exposes vault writes

`/api/mcp-dashboard` is gated by the outer middleware (`main.py:270`), which
accepts the static `DASHBOARD_MCP_TOKEN` **or any authenticated identity, with no
minimum role**; the MCP tools carry no role check. The code comment claiming the
transport "handles auth on each JSON-RPC call" (`server.py:373`) is false —
`TransportSecuritySettings` only does DNS-rebind/Host checks. So a viewer (or
anyone with the single static token) can `list_secrets_metadata` (enumerate all
secret names/types), read instances/errors/messages, and **`write_note` /
`append_note`** into the vault. Path traversal is correctly blocked by
`notes/storage.resolve()`. Fix: enforce a role floor on MCP tool invocation,
scope the static token to read-only tools, correct the comment.

### 9. Community-template HostConfig is unvalidated

Even without the #3 injection, a community-template JSON is passed to
`containers.create()` with zero HostConfig validation
(`templates.py:492-608`) — a template may declare `Privileged`, `Binds: ["/:/host"]`,
`PidMode: "host"`, `CapAdd`, `Devices`. So a single JSON file under
`/app/data/templates/` is host-root-equivalent. Fix: allowlist permitted config
keys; reject privileged/host-mount/host-namespace fields for community templates.

### 10. Prompt-injection → state-changing assistant tools

The chat LLM is given state-changing tools — `trigger_workflow`,
`set_workflow_active`, `import_workflow` (`assistant/tools.py:184-273`),
`workspace_write`/`append`/`archive` (`workspace_tools.py`) — while the same path
ingests attacker-influenceable content (RAG results, MCP output, n8n error
messages/execution payloads). A poisoned error string or knowledge doc can steer
the model to activate/trigger/import workflows or overwrite workspace files with
no confirmation. Bounded to the active instance + vault (not arbitrary host FS).
Fix: require HITL confirmation for write/trigger tools (the pattern already
exists in agent_fleet), or split read/write toolsets.

### 11. Notes search snippet rendered unescaped

`notes.js:205` escapes titles and paths but not `item.snippet`, which is derived
from note body content. A note authored by any write-capable user (or by an
agent/MCP writing to the vault) yields stored XSS when its snippet appears in
another user's search results.

---

## Low / informational

- **12. OTLP ingest open when enabled** (`main.py:234`) — unset `AGD_OTEL_TOKEN`
  returns open. Documented trusted-LAN posture; off unless
  `AGD_OTEL_ENABLED=true`. Recommend defaulting to token-required or a loud
  startup warning like the `AGD_DISABLE_LOGIN` one.
- **13. API key `==` compare** (`api_keys.py:70`) — compares sha256 hashes, so no
  usable preimage leaks; switch to `hmac.compare_digest` for consistency.
- **14. Music routes lack role granularity** (`music_router.py`) — any viewer can
  mutate music config and read `triggers.token` (which drives the
  self-authenticating `/fire` endpoint). Impact confined to the player.
- **15. TLS flag not honored in n8n_credentials** — three httpx clients omit
  `verify=`, defaulting to `verify=True`. Fail-secure but inconsistent; the
  mirror path won't reach a self-signed LAN n8n with `AGD_TLS_VERIFY=false`.
- **16. Community-module static served pre-auth** (`static_router.py:39`) — outside
  `/api/`, so not behind the gate. Traversal is blocked; only intended-public
  frontend assets are reachable.
- **17. TOTP intra-window replay** (`totp.py:55`) — a valid code is reusable
  within its 30s window; standard, low risk.
- **18. Path-segment IDs not URL-encoded** into the n8n API (`client.py:509`) —
  negligible; the caller already holds the instance's API key.
- **19. Plaintext secrets in template_state** (`deployer.py:55-148`) — scraped n8n
  keys / minted bundle secrets persist as plaintext JSON (chmod 600) rather than
  in the Fernet store. Not exploitable alone.

---

## What was checked and found sound

- **Auth core** — PBKDF2-SHA256 @ 600k + per-user salt, login-time rehash,
  `compare_digest` verify; sessions store only `sha256(token)`; TOTP secret
  Fernet-encrypted, constant-time uniform-timing verify, single-use recovery
  codes; reset tokens single-use/short-lived/hashed; `/forgot` uniform-200 with
  per-IP throttle (no enumeration/timing oracle).
- **SQL** — fully parameterized. The only f-string SQL (`errors/collector.py`
  `datetime('now','{modifier}')`) draws `modifier` from a hardcoded `_RANGE_SQL`
  allowlist; dynamic UPDATE column names come from Pydantic model fields, not
  attacker keys. No injection.
- **CSRF / CORS / WS** — double-submit CSRF over `SameSite=Strict` cookies,
  correctly skipping bearer/API-key callers; CORS default `*` with
  `allow_credentials` unset (browsers won't attach cookies cross-origin); `/ws`
  gated by session cookie or edge identity.
- **Path traversal** — notes vault, themes, `serve_js`, and module static all
  resolve + `relative_to` the base; notes additionally forbids `..`, backslashes,
  null bytes.
- **Admin / secret confidentiality** — `admin` router is `require_role("admin")`;
  secret listing returns masked hints only; `public_user()` strips
  hash/salt/TOTP; API keys returned once at creation, stored as sha256 only.
- **Fleet Health contribution API (this branch)** — contributed rows treated as
  untrusted: schema validation, row/metric caps, and `detail_url` restricted to
  in-app absolute paths (`^/[A-Za-z0-9/_-]*$`), blocking `javascript:`/`//host`/
  external — closing the obvious stored-XSS vector. No unauthenticated push
  endpoint.
- **Frontend safe spots** — `api.js` double-submit CSRF with a global `fetch`
  monkey-patch; `toast.js` uses `textContent`; `error-item.js` layers
  `esc()`/`attr()`/`jsStr()`/`encodeURIComponent` (guarded by a regression test);
  `community-modules.js` postMessage uses source-identity checks and same-origin
  `/api/` restriction; player embed sanitizer rejects non-http(s) schemes and
  strips `on*` handlers.
- **Docker module** — no shell/subprocess use (all via aiodocker HTTP API);
  `_guard_not_self` blocks self-destruction; `template_state._safe_segment`
  neutralizes traversal.

## Recommended remediation order

1. **Add the missing role floors** (#1, #2, #8, #14). Highest leverage — one
   change per router closes code-exec, SSRF, and secret-enumeration reachable by
   the read-only `viewer` role.
2. **Fix the Docker template injection + HostConfig allowlist** (#3, #9) so an
   operator cannot escalate a benign template to a privileged host-mount.
3. **Centralize a server-side-fetch SSRF guard** (#2, #7, and the knowledge/RAG
   fetch paths) that blocks internal ranges, and apply it everywhere an
   operator-supplied URL is fetched.
4. **Consolidate markdown rendering** (#4, #11) behind one escape-first (or
   DOMPurify) helper and delete the three unsafe renderers.
5. **Redact/segment secrets in `inspect`** and rebind secret scope so a URL
   repoint can't defeat it (#5, #6).
6. Low-severity hygiene: OTLP default-token, `compare_digest` for API keys, TLS
   flag consistency (#12, #13, #15).
