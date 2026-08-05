# AgeniusDesk CE — Security Fix Plan (for Opus)

**Date:** 2026-07-16
**Source review:** `docs/code-review/2026-07-16-full-security-review.md`
**Status of source review:** Verified against code. Several CONFIRMED findings did not hold up. This plan is the corrected, implementable version. Trust this file over the source review where they disagree.

Read this whole file before touching code. Section A lists what to actually build, in priority order. Section B records which source findings were downgraded or rejected and why, so you do not "fix" a non-bug and break working behavior.

---

## Verification summary

I re-traced every CONFIRMED finding to the code. Result:

| # | Source finding | Source sev | Verified verdict | Real sev |
|---|---|---|---|---|
| 1 | Docker socket RCE via deploy HostConfig | Critical | **Rejected as written** | Low (hardening) |
| 2 | SSRF via n8n instance URL | Critical | Partly true, actor is operator not unauth | Medium |
| 3 | Secret store no authz | Critical | **Overstated** — admin-gated, values masked | Low (hardening) |
| 4 | Prompt injection to tool execution | Critical | **Confirmed**, examples wrong | High |
| 5 | Public API key no limit/scope/audit | High | **Confirmed** | Medium |
| 6 | Dashboard MCP no auth by default | High | **Rejected** — gated at operator+ in middleware | Low |
| 7 | CORS `*` with credentials | High | **Rejected** — `allow_credentials` is not set | Low |
| 8 | Webhook/OTel unauth + stored XSS | High | XSS **false** (textContent); unauth ingest true but mitigated | Medium |
| 9 | SQL injection in search | Plausible | **Rejected** — whitelist-mapped, parameterized | None |
| 10 | OTel exhaustion/poisoning | Medium | Partly true, disabled by default | Medium |
| 11-20 | Auth/session defense-in-depth | Low/Med | Plausible, line numbers unverified | Low |

Net: **1 High**, **3 Medium**, the rest defense-in-depth. No confirmed pre-auth critical. The app already ships real mitigations the source review missed: `require_internal_api_auth` middleware (all `/api/*` private by default), `limit_request_size`, `security_headers` (X-Frame-Options, opt-in CSP), `csrf_protect` double-submit, constant-time token compares, masked secret display, and whitelist-based SQL filters.

---

## Section A — Fixes to implement (priority order)

### A1. [HIGH] Assistant executes state-changing tools on untrusted content with no confirmation

**This is the real finding.** Everything else is hardening.

**What is true:**
- `backend/modules/assistant/tools.py` defines `_STATE_CHANGING_TOOLS = {trigger_workflow, set_workflow_active, import_workflow, ...}` (line ~21) and `execute_tool` (line ~205) runs them directly. There is an audit line (`_audit_state_change`, line ~28) but no human confirmation gate.
- The assistant ingests attacker-influenceable content: n8n error payloads, execution `runData`, RAG/Qdrant payloads, and MCP tool outputs. The only injection defense is textual (`_ASSISTANT_INJECTION_GUARD` in `providers.py`). A model cannot reliably distinguish "user asked" from "tool output told me to."
- MCP tools reachable from the assistant can make arbitrary external calls (exfil channel). Private data + untrusted content + exfil channel = the lethal trifecta.

**What the source review got wrong:** the `rm -rf /` example is bogus — `trigger_workflow` takes `workflow_id` + `payload`, not a shell. Do not cite it. The real damage is: silently activate/deactivate/trigger/import workflows, and drive MCP tools, on injected instructions.

**Fix (do the tractable parts, in order):**
1. **Confirmation gate for state-changing tools.** When a tool in `_STATE_CHANGING_TOOLS` (and any MCP tool) is selected during an assistant turn, do not execute. Return a structured proposal `{tool, arguments, reasoning}` to the frontend. The assistant dock renders it with a Confirm button; execution only happens on an explicit user click that carries the session CSRF token. Add a settings flag `AGD_ASSISTANT_AUTORUN` (default `false`) to preserve the current auto-run behavior for operators who opt in.
2. **Keep and widen the audit log.** `_audit_state_change` should log actor identity (from `current_user`), tool, arguments (secret-scrubbed), and outcome, to the existing audit sink. Make sure it fires on the confirmed-execution path too, not just selection.
3. **Do not remove `_ASSISTANT_INJECTION_GUARD`** (the source review said to). It is cheap defense-in-depth. It is just not sufficient alone — the confirmation gate is the real boundary.
4. **MCP tool scoping (stretch):** ensure assistant-invoked MCP tools cannot read the dashboard secret store implicitly. Pass only explicit arguments. Full subprocess sandboxing is out of scope for this pass — note it as a follow-up.

**Do not** attempt the source review's "two-pass data-flow separation" rewrite in this pass. It is a large refactor; the confirmation gate delivers most of the safety at a fraction of the risk.

**Files:** `backend/modules/assistant/tools.py`, `providers.py`, `router.py`, `frontend/js/components/assistant-dock.js`, `backend/config.py` (new flag).

---

### A2. [MEDIUM] Public API keys: no rate limit, coarse scopes, no expiry, no usage audit

**Verified true.** `backend/modules/public_api/api_keys.py` + `auth.py`: keys are `agd_ + token_urlsafe(32)` (good entropy), sha256-stored, constant-time compared (all good). But: only `read`/`trigger` scopes, no expiry, no per-key IP/instance/workflow scoping, no rate limit, no usage log.

**Fix:**
1. Add optional fields to the key record: `expires_at`, `allowed_ips` (CIDR list), `allowed_instances`, `allowed_workflows`. Enforce in `verify_api_key` / `require_scope`. Absent field = unrestricted (backward compatible).
2. Add a token-bucket rate limiter keyed by `key_id` (default e.g. 120 req/min, configurable via `AGD_PUBLIC_API_RATE`). In-memory is acceptable for CE (single process); document that it is per-process.
3. Add a usage audit line per request: `key_id, endpoint, status, ts`. Reuse the existing audit sink.
4. Enforce `expires_at` in `lookup_by_hash` or the dependency (reject expired with 401).

**Files:** `backend/modules/public_api/api_keys.py`, `auth.py`, `router.py`, admin UI for key creation if it surfaces the new fields.

---

### A3. [MEDIUM] Outbound egress: global TLS flag + private-range fetches

**What is true:** `backend/net.py:assert_safe_probe_url` blocks metadata/link-local/multicast/reserved but intentionally allows all RFC1918 (self-hosted n8n/Ollama/Qdrant live there). `tls_verify()` is a single global `AGD_TLS_VERIFY` applied to every client. An operator who can add instances can point fleet-health at internal hosts and read a reachable/auth-fail/refused oracle.

**What the source review got wrong:** actor is **operator** (adding an instance needs operator role), not unauthenticated. There is no webhook path that adds instances.

**Fix (hardening, keep LAN use working):**
1. **Per-instance TLS verify.** Add `tls_verify` (bool, default true) to the instance config schema. Thread it into the n8n client factory so `AGD_TLS_VERIFY=false` is no longer required globally to talk to one self-signed box. Keep the global env as the fallback default.
2. **Optional egress allowlist.** Add `AGD_EGRESS_ALLOW_CIDRS` (comma-separated). When set, `assert_safe_probe_url` additionally requires the resolved IP to fall inside one of the CIDRs. When unset, current behavior (private ranges allowed) is preserved so existing installs do not break.
3. Route any outbound fetch that still bypasses `assert_safe_probe_url` through it. Audit `n8n_proxy/client.py`, `knowledge/backends.py`, `assistant`, `backups/remote.py` for direct `httpx` calls that skip the guard.

**Do not** hard-block private ranges by default — that breaks the product's core use case.

**Files:** `backend/net.py`, `backend/config.py`, `backend/modules/n8n_proxy/client.py`, and any client factory.

---

### A4. [MEDIUM] Unauthenticated ingestion (messages/errors webhooks, OTel): no rate limit, cost-data poisoning

**What is true:**
- `/api/errors/webhook` and `/api/messages/webhook` are in `_LEGACY_WEBHOOK_EXACT`; open unless `AGD_WEBHOOK_TOKEN` is set (default empty → open). Mitigated by `limit_request_size` but not rate-limited: a flood fills SQLite.
- `/api/otel/v1/traces` requires `AGD_OTEL_ENABLED=true` (default **false**) and is token-checked when `AGD_OTEL_TOKEN` set. When enabled without a token it is open; span `attributes_json` (model, tokens_in, cost_usd) is attacker-settable → poisons the Spend/cost dashboards; `otel_instance_map` can be poisoned to misattribute.

**What the source review got wrong:** the stored-XSS claim is **false**. Message toasts render via `toast()` which sets `el.textContent` (`frontend/js/components/toast.js:10`); the get-started list escapes with `esc()`. No unescaped sink on that path. Do not add HTML sanitization to fix an XSS that does not exist — but do keep an eye out if any future view switches these to `innerHTML`.

**Fix:**
1. Token-bucket rate limit on the three ingest paths (per-IP and per-token). Configurable cap, sane default (e.g. 600/min).
2. Prune-before-insert on `otel_spans` when over the row cap (currently prunes after insert).
3. Validate span attributes against an allowlist of expected keys/types before trusting `cost_usd`/`tokens_*` for aggregation. Reject or null out unexpected numeric fields rather than summing attacker input.
4. Consider defaulting `AGD_WEBHOOK_TOKEN` warnings at startup (warn if ingest paths are open) rather than forcing a token (forcing breaks existing installs). Match the existing startup-warning pattern.

**Files:** `backend/main.py` (rate-limit middleware or per-route), `backend/modules/observability/`, `backend/modules/errors/router.py`, `backend/modules/messages/router.py`, `backend/database.py` (prune order).

---

### A5. [LOW] Defense-in-depth cleanups (batch)

Do these only after A1-A4. Each is small and independently shippable.

1. **Docker central HostConfig sanitizer.** `_assert_safe_community_hostconfig` (`templates.py:643`) only runs on community templates at load. Extract the same checks into a helper and call it in the deployer immediately before every container-create (`deployer.py` create paths, `bundle.py`) as belt-and-suspenders. Built-in templates are code-authored and already safe, so this is purely to catch a future regression. **Not** a live RCE fix — see B1.
2. **Secret resolution audit + drop `os.environ` population.** `promote_to_secret` / `migrate_inline_to_secrets` (`config.py:598`, `:664`) write decrypted values into `os.environ`, exposing them to any process env dump. Resolve on demand instead. Add an audit line when `_resolve_secret_ref` resolves a compound secret's full field set (bare `$NAME` returning all fields as JSON, `config.py:422`). This is hardening, not an authz bug — admin routes already mask (see B3).
3. **Tighten default CORS.** `agd_cors_origins` defaults to `"*"` (`config.py:123`, `main.py:200`). `allow_credentials` is unset (False), so this is not the credential leak the source review claimed (see B7). Still, change the default to same-origin (empty list) and require operators to opt into cross-origin explicitly. Verify no bundled frontend flow depends on `*`.
4. **Constant-time MCP ping compare.** `dashboard_mcp/server.py:387` compares the ping token with `!=`. Use `hmac.compare_digest`. Minor; the real JSON-RPC path is already constant-time in `main.py:_dashboard_mcp_token_ok`.
5. **Auth session items (source findings 11-14, 17-20).** Line numbers there are unverified — re-locate before editing. Worth doing: `__Host-` prefix on the session cookie when served over HTTPS; separate lockout counters for password vs TOTP; rate-limit password-reset consume. Verify each against current `auth/service.py` before changing; the source review's line numbers drifted.

---

## Section B — Rejected / downgraded findings (do NOT implement as written)

Recording these so you do not waste effort or break working code chasing the source review.

**B1 — Docker RCE via deploy request (source #1): REJECTED as Critical.**
`POST /api/containers/deploy` (`router.py:587`) accepts only `{template_id, fields}` — **no request-supplied HostConfig**. The template is resolved server-side. `fields` are substituted into string leaves only (`_apply_subs`, `templates.py:671`), which cannot inject JSON structure like `Privileged` or `Binds`. Community templates are validated at **every** load and unsafe ones are skipped (`templates.py:846`); they live on disk at `/app/data/templates/` with no API that writes them. Built-in templates are authored in code. Router is `operator`-gated (`router.py:26`), not viewer. There is no traced exploit path. Keep only the defense-in-depth sanitizer in A5.1.

**B2 — SSRF actor: DOWNGRADED.** Real, but operator-scoped, not unauthenticated. Handled as A3.

**B3 — Secret store no authz (source #3): OVERSTATED.** The admin secrets router is `require_role("admin")` (`admin/router.py:32`) and returns only masked hints via `_hint_for` (`:129`), never plaintext. `_resolve_secret_ref` is internal runtime resolution for outbound `$REF` substitution, not a user-facing decrypt endpoint. No low-priv route decrypts arbitrary secrets. The only real bit is `os.environ` population — handled as A5.2. Do not build the proposed per-caller `require_secret_access` framework; there is no exposed boundary it would protect.

**B6 — Dashboard MCP no auth (source #6): REJECTED.** The `require_internal_api_auth` middleware (`main.py:308`) gates `/api/mcp-dashboard`: static `DASHBOARD_MCP_TOKEN` (constant-time) OR operator+ browser identity. Open only if the operator disables login entirely (`AGD_DISABLE_LOGIN=true`), which is not the default (`agd_disable_login=False`). `write_note`/`append_note`/`list_secrets_metadata` therefore require operator. The source review's claim that "only the ping endpoint validates the token" is wrong — it read the in-handler `ping` check and missed the middleware. Only A5.4 (ping constant-time) remains.

**B7 — CORS with credentials (source #7): REJECTED.** `allow_credentials` is never passed to `CORSMiddleware` (`main.py:207`), so it defaults False. `allow_origins=["*"]` + `allow_credentials=False` is the safe combination: browsers will not attach cookies cross-origin and the response cannot carry credentials. No credential-theft path. Default tightening remains as A5.3 hygiene only.

**B8-XSS — Stored XSS via toasts (part of source #8): REJECTED.** `toast()` uses `el.textContent` (`toast.js:10`). No unescaped sink. The unauth-ingestion / rate-limit half is real and handled as A4.

**B9 — SQL injection (source #9): REJECTED.** The f-string SQL in `errors/collector.py` interpolates only `_range_modifier()` output, which is a whitelist dict lookup (`_RANGE_SQL.get`, `:79`/`:95`); all values are bound with `?` placeholders. `database.py` f-strings interpolate internal column-name constants during migrations, never user input. No injection path found.

---

## Section C — Suggested sequencing

1. A1 (assistant confirmation gate) — highest real risk, biggest design surface. Land behind `AGD_ASSISTANT_AUTORUN=false` default.
2. A2 (public API hardening) — self-contained.
3. A4 (ingestion rate limit + OTel attribute validation) — self-contained.
4. A3 (per-instance TLS + optional egress allowlist) — touches the client factory; test LAN n8n still reachable.
5. A5 batch (defense-in-depth) — after the above.

Every change must preserve existing default behavior for current installs (new restrictions opt-in, or default-off with a startup warning). Run the existing test suite and `ruff` (line-length 120) before proposing the diff. For A1 and A3, exercise the real flow end to end, not just unit tests.
