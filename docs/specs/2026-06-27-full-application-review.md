# Full-Application Review — AgeniusDesk CE

Date: 2026-06-27
Reviewer: Copilot (fresh-eyes pass, outside the isolation review chain)
Scope: the whole `main` tree, with emphasis on the surfaces Michael called out
(auth/CSRF, secret store, Docker-socket exposure) and anything outside the
isolation work. The isolation work itself (phases 1-2) is summarized at the end;
it already went through 5 adversarial passes and is not re-litigated here.

Baseline at review time: 177 tests pass (`uv run pytest`), ruff clean. Five
local unpushed commits implement out-of-process backend isolation (dormant by
default).

---

## Summary

The app is in solid shape for a v0.2.x community release. The auth gate, CSRF,
session model, and secret encryption are well-designed and match the prior auth
spec review. The isolation work is sound and dormant.

The findings below are **pre-existing** (not introduced by the isolation
commits). The headline theme: the internal-API auth middleware in `main.py` is
the **single** auth chokepoint, and a large number of routers attach no
per-router role dependency. That is safe today because the middleware is
fail-closed, but it is a defense-in-depth gap, and on a few routers it produces
a real privilege issue: a **viewer** (the lowest role) can reach write surfaces
that should require operator or admin.

Severity scale used below: HIGH (ship blocker / privilege issue), MEDIUM (should
fix before or soon after release), LOW (hardening / follow-up).

---

## Findings

### 1. HIGH — `n8n_credentials` router has no role gate; viewer can mirror secrets to n8n

**Location:** `backend/modules/n8n_credentials/router.py:39`

```python
router = APIRouter(prefix="/api/n8n-credentials", tags=["n8n-credentials"])
```

No `dependencies=[Depends(require_role(...))]`. Every other sensitive router
either gates at the router level (`admin` → admin, `n8n_proxy`/`docker_mgr` →
operator, `assistant` → operator floor + admin bar on writes) or is read-only.
This one is neither.

**Why it matters:** `POST /api/n8n-credentials/{instance_id}/mirror` resolves
`$SECRET_NAME` to its decrypted value and POSTs it to an n8n instance the caller
chooses. A viewer-authenticated identity (the lowest role) can:

- drive the mirror to push stored secrets into an n8n instance they control
  (exfiltration channel), and
- read `GET /{instance_id}/mappings`, which fetches live credential schemas from
  the instance (a low-grade SSRF/recon surface into the n8n API).

The per-secret instance scope (`is_secret_allowed_on_instance`) is checked, but
that gates *which* secret, not *who* can trigger the push.

**Fix:** gate the router at operator (mirror is a write that spends secrets and
mutates n8n state):

```python
from backend.auth_gate import require_role
router = APIRouter(
    prefix="/api/n8n-credentials", tags=["n8n-credentials"],
    dependencies=[Depends(require_role("operator"))],
)
```

Add a regression test mirroring `tests/test_assistant_authz_ssrf.py` (viewer →
403, operator → not 403).

### 2. MEDIUM — `knowledge` router has no role gate; viewer can mutate knowledge config

**Location:** `backend/modules/knowledge/router.py:37`

No role dependency. `POST /sources`, `PUT /sources/{id}`, `DELETE /sources/{id}`,
`PUT /connectors/{id}`, `PUT /instructions` are all config writes reachable by a
viewer. `POST /sources/{id}/test` probes an operator-supplied backend URL
(Qdrant/MCP), which is SSRF-adjacent (the URL comes from the source config the
viewer can also set).

**Fix:** operator floor on the router, admin bar on the destructive writes, or
at minimum operator on the whole router. The `test` endpoint should reuse the
`assert_safe_probe_url` guard from `assistant/providers.py` (or a shared helper)
before fetching.

### 3. MEDIUM — `messages` router: viewer can delete/clear all messages

**Location:** `backend/modules/messages/router.py`

No role dependency. `DELETE /{message_id}` and `DELETE ?before_date=` (clear all)
are reachable by a viewer. The `POST /webhook` is correctly token-gated by the
middleware (`_LEGACY_WEBHOOK_EXACT`). Reads are fine for any authenticated user,
but bulk delete is an operator action.

**Fix:** `Depends(require_role("operator"))` on the two DELETE routes (or the
whole router, with the webhook route exempted — it's already middleware-gated).

### 4. MEDIUM — `themes` write routes reachable by viewer

**Location:** `backend/modules/themes/router.py:12`

No role dependency. `POST /` (save custom theme) and `POST /active/{theme_id}`
(set active theme = config write to `config.json`) are reachable by a viewer.
Path traversal is correctly guarded (`_safe_theme_path` + regex), so this is a
privilege issue, not an escape. Low blast radius (cosmetic), but it's a config
mutation.

**Fix:** operator floor on the two POST routes. Reads stay open to any
authenticated user.

### 5. MEDIUM — single chokepoint: too many routers rely only on the middleware gate

This is the structural finding behind #1-4. The middleware in `main.py`
(`require_internal_api_auth`) is fail-closed and correct, but it only checks
"some identity present." Role enforcement is left to each router, and a majority
of routers attach no `require_role` dependency:

- `insights`, `observability` (query side), `notes`, `player`, `knowledge`,
  `messages`, `themes`, `n8n_credentials` — all rely solely on the middleware.

For read-only routers (`insights`, `observability` GET, `notes` reads) that's
defensible. For write routers it's a privilege gap (see #1-4). The risk is also
**regression concentration**: if a future change adds a new public prefix or
exempts a path in `_PUBLIC_API_PREFIXES`, every ungated router opens at once.

**Fix (recommended, not a ship blocker):** adopt a convention that every router
with any non-GET handler attaches `Depends(require_role(...))` at the router
level. Reads can stay at viewer; writes at operator; destructive/config at
admin. This makes the auth posture auditable per-file instead of implicit.

### 6. LOW — OTel metrics ingest path is allowlisted but not implemented

**Location:** `backend/main.py` (`_OTEL_INGEST_EXACT` includes
`/api/otel/v1/metrics`) and `backend/modules/observability/router.py` (only
`/v1/traces` is defined).

The middleware lets `/api/otel/v1/metrics` through the token check, but the
router has no `/v1/metrics` handler, so it 404s. Not exploitable, just dead
surface. Either implement the metrics receiver or drop the path from the
allowlist until it exists.

### 7. LOW — legacy `enc:` secret format still decryptable, no forced re-encrypt

**Location:** `backend/config.py` (`_LEGACY_PREFIX = "enc:"`, `decrypt_value`)

The legacy XOR-stream format is unauthenticated and broken under key rotation.
`decrypt_value` falls back to it for old installs and logs a migration warning,
but there's no background sweep that re-encrypts legacy values to `fernet:` on
read or on startup. A long-running install can carry `enc:` values indefinitely.

**Fix:** on `decrypt_value` of an `enc:` value, re-encrypt to `fernet:` and
persist (best-effort) so the legacy path ages out. Low priority; the format is
read-only and the warning is logged.

### 8. LOW — `agd_cors_origins` defaults to `*` and the middleware allows all methods/headers

**Location:** `backend/main.py` `_cors_origins()` + `CORSMiddleware`

Default `*` is convenient for self-hosted first-run but means a malicious page
in any origin can issue requests to the API (the session cookie is
`SameSite=Strict`, which blunts CSRF, but bearer-token/API-key callers are still
exposed if a user pastes a key into a hostile page). Document the production
recommendation (set `AGD_CORS_ORIGINS` to the real origin list) in
`.env.example` and `docs/CONFIG.md` — both are sparse today (flagged in the
prior auth spec review as items #5 and #6, still open).

### 9. LOW — `.env.example` and `docs/CONFIG.md` are minimal

Carried forward from the auth spec review. The new env vars
(`AGD_DISABLE_LOGIN`, `AGD_SESSION_TTL_DAYS`, `AGD_OTEL_*`, `AGD_CSP`,
`AGD_MODULE_ISOLATION`, etc.) are not documented in either file. Operators
discovering the app via `.env.example` won't know the security knobs exist.

---

## What's well done

- **Auth gate design.** Three-source precedence (session > edge > admin token),
  uniform `current_user()` return shape, `require_role` with role ranking,
  fail-closed middleware. The prior auth spec's 10 issues are largely resolved
  (600k PBKDF2, `USERS_FILE`/`DB_FILE` in `harden_file_permissions`, WS cookie
  gate, CSRF middleware ordering).
- **CSRF.** Double-submit, correctly skips bearer/API-key callers, exempts the
  auth-bootstrap endpoints (no valid session yet). The exemption reasoning is
  documented inline.
- **Sessions.** SHA256 of token only in DB, sliding + absolute cap,
  HttpOnly + SameSite=Strict, rotation on privilege transitions, per-username
  AND per-IP lockout, single-use reset tokens.
- **Secret store.** Fernet (authenticated), pass-through for `$NAME` refs and
  already-encrypted values (no double-wrap), compound typed secrets. API keys
  stored as SHA256 only (no raw replay).
- **Module installer.** Two-phase inspect/install with resolved-SHA pinning
  (rejects swapped tags), tarball extraction with link/special/traversal
  rejection + 3.12 `data` filter, `_safe_community_dir` containment check on
  every destructive op, AST capability scanner with proportional consent
  friction. The path-traversal hardening (no dots in module id, Windows reserved
  name rejection) is solid.
- **Docker socket.** `docker_mgr` gates at operator. The compose file documents
  the socket = host-root risk inline. The real exposure (dashboard compromise →
  host root via socket) is exactly what the isolation work mitigates for
  community modules.
- **SSRF guard.** `assert_safe_probe_url` resolves hostnames and blocks
  link-local/multicast/reserved/unspecified. Correctly allows loopback/LAN
  (legit Ollama). Has regression tests.
- **Notes/workspace storage.** `resolve()` rejects `..`, absolute paths,
  backslashes, null bytes, and asserts the resolved path stays under the vault
  root. Archive is soft-delete only.

---

## Isolation work (phases 1-2) — summary

Not re-reviewed in depth (5 prior passes). Confirming the current state:

- **Review-5 blocker is fixed.** `_parse_cmdline_string` returns `None` on
  `shlex.ValueError` (was the open item). The orphan sweep now correctly treats
  ambiguous command lines as "cannot verify → skip kill."
- **Default off.** `AGD_MODULE_ISOLATION=in_process` is the default; the entire
  subprocess path is dormant. Existing installs and the 3066 deployment are
  unchanged. Confirmed in `backend/modules/__init__.py:_isolation_mode()`.
- **Env allowlist** (`agd_module_worker/sandbox.py`): allowlist, not blocklist.
  `is_secret_like` is a secondary guard on operator-allowlisted names. Correct.
- **Import blocker** (`HostImportBlocker`): `sys.meta_path` finder that raises
  on `import backend`. This is the real guarantee; `curate_sys_path` is
  defense-in-depth (denylist by design, documented why).
- **Proxy header hygiene** (`proxy.py`): strips `Cookie`/`Authorization`/`host`
  on the request, `Set-Cookie`/`Set-Cookie2`/`Clear-Site-Data`/`WWW-Authenticate`
  on the response. Hop-by-hop set correct.
- **PID-reuse safety** (`supervisor.py`): exact adjacent-token argv match
  (`--agd-module <id>`), returns False on unreadable argv. Sound.
- **Threat model honesty.** Spec Section 3 correctly frames phase-2 as "raised
  bar," not OS-user containment. The container tier is the real boundary.

The isolation work is approved from this pass. No new findings.

---

## Decisions (Michael's two open questions)

### 1. Push the 5 local commits to origin/main, or hold?

**Push.** The isolation work is sound, dormant by default, and the full-app
findings above are pre-existing (present on `origin/main` before the isolation
commits). Holding the isolation commits doesn't reduce risk; it just delays the
reviewed work. The findings in this doc can land as follow-up commits on
`main` (they're small, mostly one-line `Depends(...)` additions + tests).

### 2. Phase 3 (host bridge) as the next build?

**Yes, it's the right next step.** The current gap (a module that imports
`backend.*` can't run under isolation) is correctly deferred and correctly
documented. Phase 3 is what makes isolation usable for real modules. Two
suggestions for the phase-3 spec:

- The host bridge (`/api/_host/*`, `notes.*`, `assistant.complete`) becomes the
  **new** SSRF/RCE surface a sandboxed module can reach. Design it as an
  explicit capability surface with per-module allowlisting, not a generic
  pass-through. A module should get only the host calls it declares in its
  manifest.
- The bridge is also where the env-allowlist story completes: today
  `forward_env=[]` means an isolated module gets no config. Phase 3 should
  define how a module receives its declared secrets (injected via the bridge
  call, not via inherited env), so the allowlist stays tight.

---

## Recommended fix order

1. **#1 (n8n_credentials role gate)** — one line + test. Highest priority; it's
   a real viewer→secret-exfil path.
2. **#2, #3, #4 (knowledge / messages / themes role gates)** — small, same
   pattern.
3. **#5 (convention)** — apply `require_role` to remaining write routers as a
   sweep.
4. **#9 (.env.example / CONFIG.md)** — document the security knobs.
5. **#6, #7, #8** — low-priority hardening.
