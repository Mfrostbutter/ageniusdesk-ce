# Cross-Module Code + Security Review — Community Modules

Date: 2026-07-01
Scope: all 20 built-in modules under `backend/modules/` plus the module spec
(`docs/architecture/modules.md`, `manifest.json` schema, the installer/scanner).
Method: five parallel per-module deep reads against one shared rubric (RBAC floor,
outbound/SSRF, filesystem, subprocess, secret handling, SQL, input validation,
manifest-vs-code capability gap), then manual verification of every ranked finding.

This is a **comparative** review: the goal is not another findings dump (the
2026-07-01 full review already did that and its four High fixes are confirmed
present) but to surface where modules **diverge from each other**, and to turn the
consistent patterns into a written standard future modules must meet.

Companion docs: [full security review](2026-07-01-full-security-review.md),
[security posture](../architecture/security.md), [module system](../architecture/modules.md).

---

## Update 2026-07-01 — remediated

All actionable findings below are fixed and covered by regression tests
(`tests/test_cross_module_review.py`, 15 tests; full suite 316 passed). Summary of
the changes:

- **C1 (root cause)** — the SSRF guard + TLS helper moved to a shared `backend/net.py`
  (`assert_safe_probe_url`, `UnsafeProbeURL`, `tls_verify`). `providers.py` re-exports
  them for back-compat; every other module imports from `backend.net` instead of
  cross-importing `assistant`.
- **S1** — `knowledge/backends.py._search_qdrant` now guards the operator URL and
  returns status-only errors (no body reflection).
- **S2** — the guard moved into `n8n_proxy/client.py.test_connection_with`, the single
  chokepoint for create / setup / test-creds. `errors` install-handler (S6) and
  `n8n_credentials` schema fetch (S5) are guarded too.
- **S7** — the assistant `custom_base_url` (via `_safe_base`) and `rag.py` qdrant URL
  are guarded; S8 (admin `/assistant/test`) is covered transitively.
- **C2** — `providers.py` (16 clients), `rag.py`, and `knowledge/backends.py` now pass
  `verify=tls_verify()`.
- **S3** — the password step no longer resets the login throttle when 2FA is enabled,
  and `/login/totp` records failures + checks the lockout, so the second factor is
  rate-limited (`auth/router.py`).
- **C3** — `modules` router now floors with `require_role("viewer")`.
- **C4** — `player/music_router.py` `/triggers/fire` uses `hmac.compare_digest`.
- **S12** — admin `create_user` runs the same `_validate_password` composition policy
  as `/auth/setup`.
- **C5/C6** — `capabilities` blocks added to `assistant` and `knowledge` as exemplars;
  `license: "MIT"` backfilled across all 19 manifests. `knowledge` also gained its
  missing `secrets_required`.
- **S14** — orphaned `health/__pycache__` bytecode removed.

Deliberately **not** changed, with rationale:

- **C7 (agent_fleet nav)** — the nav button is static HTML and *conditionally removed
  at runtime* when the `langgraph` extra is absent (`app.js`). A static
  `frontend.nav` can't express that runtime gate and would double-render, so the
  hardcoded nav stays; treat it as a documented exception, not drift.
- **docker_mgr capabilities (C5)** — ~~the `Capabilities` schema has no field for
  host-privilege / socket access~~ **CLOSED 2026-07-02.** Added a `docker` capability
  to the schema (`module_registry.Capabilities`) and scanner detection (`docker`/
  `aiodocker` imports and `/var/run/docker.sock` references → HIGH when undeclared,
  INFO when declared, since Docker-daemon access is root-equivalent). `docker_mgr` and
  `assistant` (n8n-mcp provisioning) now declare `"docker": true`. Covered by
  `tests/test_docker_capability_scanner.py`.
- **Pre-existing `E501`** long lines in `n8n_proxy` are untouched existing debt.

---

## Executive summary

The fleet is broadly consistent and the high-risk surfaces (agent_fleet in-process
exec, docker_mgr socket, the community installer/scanner) are correctly gated and
were re-verified sound. The divergences that matter are **cross-cutting conventions
applied unevenly**, not one-off bugs:

1. The **SSRF guard** (`assert_safe_probe_url`) is applied to some operator-supplied
   fetch paths and not others — the same class the last review closed for Ollama/MCP
   is still open in `knowledge`, `n8n_proxy` create/setup, and a few lower-risk paths.
2. **`AGD_TLS_VERIFY`** is honored by some outbound clients and silently ignored by
   others (`assistant/providers.py`, `assistant/rag.py`, `knowledge/backends.py`),
   contradicting the documented "all outbound httpx respect it" contract.
3. **No built-in manifest declares a `capabilities` block**, even though the spec
   makes capabilities the center of the community-module trust model. The modules a
   third-party author copies as templates model none of the thing the scanner grades
   them on.
4. Small primitive inconsistencies: one module uses `require_trusted_request` where
   every other uses `require_role`; one token compare is not constant-time; `license`
   is set on one manifest of eighteen.

No new CRITICAL or unmitigated High was found. New actionable items: one MEDIUM
(TOTP second factor has no rate limit) and the two SSRF paths already tracked as
Medium #6/#7 remain open. Everything else is LOW / consistency / spec hygiene.

---

## Comparison matrices

### RBAC floor per module

Pattern in use across the fleet (and the one this review endorses):
**read = viewer** (no explicit floor; the global `require_internal_api_auth`
middleware still requires a logged-in identity), **mutate = operator**,
**host-affecting / code-exec / secret-store = admin**.

| Module | Prefix | Router floor | Mutations | Notes |
|---|---|---|---|---|
| admin | `/api/admin` | **admin** | admin | uniform; strongest surface |
| agent_fleet | `/api/agent-fleet` | **admin** | admin | whole router; even reads exec operator code ✅ |
| assistant | `/api/assistant` | operator | admin (`/jobs /shared /config /baseline`) | escalation pattern ✅ |
| assistant (mcp) | `/api/mcp` | operator | operator | ✅ (2026-07-01 fix) |
| auth | `/api/auth` | none (bootstrap) | n/a | intentional; verified safe |
| dashboard_mcp | `/api/mcp-dashboard` | token / operator (middleware) | — | MCP token or operator identity |
| docker_mgr | `/api/containers` | **operator** | operator | operator = root-equiv via socket (see D1) |
| errors | `/api/errors` | none | operator | `/webhook` open (token) by design |
| insights | `/api/insights` | none | operator (`/refresh`) | read-only analytics |
| knowledge | `/api/knowledge` | operator | operator | ✅ |
| messages | `/api/messages` | none | operator | `/webhook` open (token) by design |
| modules | `/api/modules` | **`require_trusted_request`** | operator | ⚠️ divergent primitive (see C3) |
| n8n_credentials | `/api/n8n-credentials` | operator | operator | ✅ |
| n8n_proxy | `/api/n8n` | operator | operator | ✅ |
| notes | `/api/notes` | none | operator | ✅ |
| observability | `/api/otel` | none | operator (`/pricing/refresh`) | `/v1/traces` open (token) by design |
| player (spotify) | `/api/spotify` | operator | operator | ✅ |
| player (music) | `/api/music` | none | operator | `/triggers/fire` self-auth token |
| public_api | `/api/v1` | none | per-route `X-API-Key` scope | read/trigger scopes ✅ |
| themes | `/api/themes` | none | operator | ✅ |
| webhooks / health | — | not mounted | — | placeholders, no `router` exported |

Verdict: floors are consistent **except** `modules` (C3). Every mutating endpoint
has a floor; the only unfloored writes are the three by-design machine-ingest
webhooks (`errors`, `messages`, `observability`), each token-gated in middleware.

### Outbound network + SSRF guard + TLS verify

`assert_safe_probe_url()` (currently living in `assistant/providers.py`) blocks
metadata/link-local/reserved on operator-supplied fetch targets. `_verify()` (in
`n8n_proxy/client.py`) reads `AGD_TLS_VERIFY`.

| Module / path | Operator-supplied URL fetched? | `assert_safe_probe_url`? | Honors `AGD_TLS_VERIFY`? |
|---|---|---|---|
| assistant · Ollama | yes | ✅ | ✅ (mcp) / ⚠️ (providers) |
| assistant · MCP servers | yes | ✅ (`_normalize_mcp_urls`) | ✅ |
| assistant · custom_base_url | yes (admin) | ❌ | ❌ |
| assistant · rag qdrant_url | yes (admin) | ❌ | ❌ |
| assistant · providers (LLM APIs) | no (fixed hosts) | n/a | ❌ |
| knowledge · qdrant `config.url` | **yes** | **❌** | **❌** |
| n8n_proxy · `/test-creds`, `/mirror` | yes | ✅ | ✅ |
| n8n_proxy · `/instances`, `/setup` | **yes** | **❌** | ✅ |
| n8n_credentials · schema fetch | yes (stored) | ❌ | ✅ |
| errors · install-handler connect | yes (stored/fresh) | ❌ | ✅ |
| observability · pricing | no (openrouter.ai) | n/a | ✅ |
| player · Spotify | no (fixed hosts) | n/a | n/a (public CA) |
| modules · GitHub tarball | no (api.github.com; redirect to codeload) | n/a (host locked) | ❌ (benign) |

Two clean gaps fall out of this table: **SSRF guard coverage (C1)** and
**TLS-verify coverage (C2)**.

### Secret handling

Consistent and sound across the board: Fernet at rest, `$NAME` refs resolved
server-side, API keys stored sha256-hashed, `hmac.compare_digest` on token checks
(one exception, C4). No module returns a full secret value in a response. Deliberate
partial/whole exposures, all operator/admin-gated and by design:
`admin` `_hint_for` (first-4+last-3 of a secret), `n8n_proxy` `/instances/{id}/login`
(decrypted n8n owner password), `player`/`music` trigger token on its GET/rotate.

### SQL

All parameterized. The two dynamic-column builders (`knowledge/storage.update_source`,
`agent_fleet/storage.update_run`) were both traced: column names come only from a
hardcoded whitelist / model fields, values are always `?`-bound. No injection.

---

## Consistency findings (the cross-comparative core)

### C1 — SSRF guard applied unevenly; and it lives in the wrong place
The guard is enforced on Ollama, MCP, and the two sensitive n8n paths, but not on
`knowledge` qdrant (S1), `n8n_proxy` create/setup (S2), `n8n_credentials` schema
fetch, `errors` install-handler, or the admin-set `custom_base_url`/`rag` URLs.
Root cause is partly architectural: `assert_safe_probe_url` lives inside the
`assistant` module, so every other module either cross-imports from `assistant` or
skips it. **Recommendation:** promote it to a shared `backend/net.py` (alongside a
shared `tls_verify()`), and make "operator-influenced URL → guard before fetch" a
checklist item every outbound path must satisfy.

### C2 — `AGD_TLS_VERIFY` not honored by all outbound clients
`assistant/providers.py`, `assistant/rag.py`, and `knowledge/backends.py` construct
`httpx.AsyncClient` with no `verify=`, so `AGD_TLS_VERIFY=false` is silently ignored
(fails closed, so not an exposure — but it breaks self-signed LAN Ollama-custom /
qdrant / embeddings, contradicting the documented contract). Fix: thread the shared
`tls_verify()` into every client.

### C3 — `modules` router uses a different auth primitive
Every module floors with `require_role(...)` except `modules/router.py:16`, which
uses `require_trusted_request` (any authenticated identity). Effect: a bare **viewer**
can read the full module inventory and the install lock (`/api/modules`,
`/nav`, `/isolation`, `/{id}`) — including source repos, `installed_sha`, and
`approved_by` usernames. No state change is reachable (every write is independently
`require_role("operator")`), so this is info-disclosure only. Standardize on
`require_role("viewer")` at the router level for parity.

### C4 — one non-constant-time token compare
`player/music_router.py:464` (`/triggers/fire`) uses `supplied != expected`; the
middleware webhook/OTel/MCP token checks all use `hmac.compare_digest`. Impractical
to exploit over the network against a 24-byte secret, but it should match the
codebase convention.

### C5 — no built-in declares a `capabilities` block
The spec makes `capabilities` (network hosts, fs `write_paths`, subprocess, env) the
heart of the community trust model: the scanner's headline output is the
declared-vs-detected diff, and an undeclared capability is a HIGH finding. Yet **zero
of the eighteen mounted built-ins declare one**, and every one of them has real
undeclared capability: network egress (all outbound modules), Docker-socket control
(`docker_mgr`, and `assistant` via n8n-mcp provisioning), fs writes under `data/`, and
env reads beyond `secrets_required` (`AGD_TLS_VERIFY`, `AGD_WEBHOOK_TOKEN`,
`OLLAMA_URL`, `AGD_OTEL_*`, …). Built-ins are exempt from the scanner, but they are
also the reference implementations a third-party author copies. **Recommendation:**
backfill `capabilities` blocks on the built-ins as canonical, correct exemplars — the
`docker_mgr`, `assistant`, and `knowledge` manifests especially, since those model
the socket / arbitrary-host cases an author most needs to see declared.

### C6 — `license` set on one manifest of eighteen
Only `agent_fleet` declares `"license": "MIT"`. The repo is MIT and the house rule is
"all new source is MIT." Add it to every manifest so the exemplar is right.

### C7 — nav contributed inconsistently
`agent_fleet` ships a UI view but declares no `frontend.nav`; its nav is wired by
hardcode in `app.js` instead of through the manifest the spec documents as the
contribution path. Low impact for a built-in, but it is the wrong example for a
community author. Declare `frontend.nav` in the manifest.

---

## Security findings (consolidated, ranked, de-duped)

Cross-referenced to the 2026-07-01 full review so nothing is double-counted.

| # | Sev | Module:line | Status vs prior review |
|---|---|---|---|
| S1 | MED–HIGH | knowledge/backends.py:102,109 | = prior Medium #7, still open |
| S2 | MEDIUM | n8n_proxy/router.py:131,231 | = prior Medium #6, still open |
| S3 | MEDIUM | auth/router.py:184–191 | **new** |
| S4 | MEDIUM | providers.py / rag.py / knowledge backends (C2) | **new** (contract gap, fails closed) |
| S5 | LOW | n8n_credentials/mappings.py:287 | new (same class as S1, stored URL) |
| S6 | LOW | errors/router.py (install-handler connect) | new (same class, stored/fresh URL) |
| S7 | LOW | assistant providers.py:495 / rag.py:34 | new (admin-set URLs, unguarded) |
| S8 | LOW | admin/router.py:377 (`/assistant/test`) | new (admin → arbitrary URL) |
| S9 | LOW | modules/router.py:16 (C3) | new (viewer info-disclosure) |
| S10 | LOW | player/music_router.py:464 (C4) | new (non-constant-time compare) |
| S11 | LOW | admin/router.py:133 (`_hint_for`) | accepted-risk (partial secret in hint) |
| S12 | LOW | admin/router.py:94 (`create_user`) | new (admin-minted pw skips composition policy) |
| S13 | LOW/INFO | docker_mgr (D1) | design note: operator = root-equiv via socket |
| S14 | housekeeping | modules/health/__pycache__/*.pyc | orphaned bytecode, not importable |

**S1 — knowledge qdrant SSRF + body reflection.** `_search_qdrant` POSTs to an
operator-supplied `config.url` with no guard and reflects `r.text[:200]` on error.
Reachable via `POST /sources/{id}/test`, `GET /search`, and the dashboard MCP
`search_knowledge`; unauthenticated on an `AGD_DISABLE_LOGIN` install. This is the
knowledge follow-up already logged as Medium #7 — the reflection + open-install reach
make a case for prioritizing it. Fix = S1 falls out of the C1 shared-guard work.

**S3 — TOTP second factor has no rate limit.** A correct password calls
`throttle_reset` (router.py:170) before the TOTP step; `/login/totp` raises on a bad
code with no `throttle_record_failure` (router.py:190). An attacker who already holds
the password can loop password→pending→guess indefinitely with the lockout counter
cleared each round, so the 6-digit second factor is bounded only by the ~90s TOTP
window, not by attempts. Add a per-user throttle on failed second-factor attempts
(and do not reset the throttle until the factor also passes).

**S13 (D1) — docker_mgr operator floor is root-equivalent.** The socket lets an
operator pull any image and create a container with any config (the HostConfig
injection path is closed — `_apply_subs` leaf-substitution confirmed present). This
is documented and intentional, but operator < admin in the role model while this
surface is effectively admin+. Worth an explicit line in the security doc / an ADR so
it is a decision, not an accident.

Confirmed sound (re-verified, no action): agent_fleet whole-router admin floor;
docker_mgr `_apply_subs`; installer tar traversal/symlink/hardlink/escape guards +
swapped-tag guard + server-side consent; scanner never imports/execs scanned code;
notes/themes path-traversal guards (regex + `resolve()`+`relative_to`); all SQL
parameterized; public_api key check on every route, constant-time, hashed at rest.

### Server-side XSS contributors (tie-off for prior #11)
`messages` (toast title/body), `observability` (span name/attributes), and `notes`
(markdown) all store attacker-influenceable text raw and hand it to a client
renderer. The four hand-rolled renderers were hardened in the 2026-07-01 pass; the
**toast renderer and the trace-waterfall renderer** should be confirmed to escape /
`textContent` (not `innerHTML`) before this class is fully closed. Frontend follow-up,
tracked with #11.

---

## Standard for future modules (built-in and community)

Turn the consistent patterns above into a checklist. A new module is not "done" until
every applicable box is ticked.

**Manifest (`manifest.json`)**
- [ ] `id`, `name`, real `version`, real `min_app_version`, `description`, `author`, `license`.
- [ ] `routes_prefix` matches the actual router prefix (`/api/{id}`).
- [ ] `secrets_required` lists every credential, with `required` set truthfully.
- [ ] `capabilities` block declared and accurate: `network.enabled` + specific `hosts`
      globs (never `enabled:true` with empty `hosts`), `filesystem.write_paths` (all
      under `data/`), `subprocess`, and every `env` key read beyond `secrets_required`.
- [ ] `frontend.nav` declared if the module has a UI view (don't hardcode nav in `app.js`).

**Router / RBAC**
- [ ] `APIRouter(prefix="/api/{id}", tags=[...])`.
- [ ] Floor by the fleet convention: read = viewer (no explicit floor), mutate =
      operator, host-affecting / operator-code-exec / secret-store = admin.
- [ ] Use `require_role(...)` — not `require_trusted_request` — as the primitive.
- [ ] Whole-surface tier → router-level `dependencies=[Depends(require_role(...))]`;
      mixed → per-endpoint, escalating the sensitive writes.
- [ ] Anything that imports/execs operator-authored code or drives the Docker socket
      with arbitrary config is **admin**.

**Outbound / SSRF / TLS**
- [ ] Every server-side fetch of an operator- or user-influenced URL calls the shared
      `assert_safe_probe_url()` **before** the request.
- [ ] Probe/test endpoints never reflect the fetched response body on error.
- [ ] Every `httpx` client passes the shared `tls_verify()` (honors `AGD_TLS_VERIFY`).

**Secrets**
- [ ] Read via `load_secrets` / `resolve_secret` / `$NAME`; never hardcode.
- [ ] Never return a secret value; names/hints only (reconsider even partial hints on
      high-sensitivity fields).
- [ ] Tokens/keys stored hashed or Fernet-encrypted; compare with `hmac.compare_digest`.

**Storage**
- [ ] All SQL parameterized; any dynamic column list is whitelisted.
- [ ] Path inputs validated by strict regex **and** `resolve()`+`relative_to(root)`
      containment (the notes/themes pattern); all writes under `data/`.

**Frontend**
- [ ] Any LLM / agent / MCP / RAG / webhook / span text rendered in the UI is escaped
      or DOMPurify-sanitized before it touches `innerHTML`.

**Tests**
- [ ] RBAC floor tests (viewer/operator/admin → expected status) for the new routes.
- [ ] SSRF-guard tests for every new outbound path (mirror
      `tests/test_assistant_authz_ssrf.py` / `test_high_severity_fixes.py`).

---

## Recommended remediation order

1. **Shared `assert_safe_probe_url` + `tls_verify` util** (`backend/net.py`), then
   route S1/S2/S5/S6/S7 and C2 through it. One change closes the whole SSRF/TLS
   consistency class. (MED)
2. **S3 TOTP throttle** — small, self-contained, real auth gap. (MED)
3. **C3** — swap `modules` router to `require_role("viewer")`. (LOW, trivial)
4. **C5/C6/C7 manifest hygiene** — backfill `capabilities`, `license`, and
   `agent_fleet` nav. Makes the built-ins correct exemplars for community authors.
5. **C4, S12, S14** — constant-time compare, admin password policy, delete orphaned
   `.pyc`. (LOW / housekeeping)
6. Adopt the standard checklist above into the module-authoring docs and PR template.
