# Bug Hunt Remediation

**Status:** SPEC. Nothing built.
**Date:** 2026-08-16
**Source:** [docs/code-review/2026-08-15-full-bug-hunt.md](../code-review/2026-08-15-full-bug-hunt.md) (63 findings: 4 P1, 17 P2, 27 P3) plus the adjudicated [adversarial review](../code-review/2026-08-15-full-bug-hunt-adversarial-review.md). Reconciled 2026-08-16 with the independent [fix spec](../code-review/2026-08-15-full-bug-hunt-fix-spec.md); this document is canonical and supersedes it.
**Baseline:** `feat/trace-backfill` tip (v0.5.0 + trace backfill Phase 1 + instance rename/rotate).

---

## Shape of the work

The hunt found three systemic root causes behind 20+ sites, plus independent backend and frontend defects. Fixing by root cause, not by bug id, keeps the diff coherent and closes whole classes at once. Five phases, each an independently shippable PR, ordered security-first. Every phase lands with tests, runs the full suite, and dogfoods on 3066 before the next starts.

Every finding in the log is dispositioned here: fixed in a phase, parked behind an open decision, or explicitly deferred with a reason. Bug ids reference the hunt log; this spec does not restate full root-cause detail.

| Phase | Theme | Bugs | Size |
| --- | --- | --- | --- |
| 1 | XSS and output encoding (S1) | 001, 002, 003, 020-sites, 060, 062 | M-L |
| 2 | Backend security hardening | 008, 009, 015, 016, 018, 021, 031, 032, 040, 043 | M |
| 3 | Fleet TLS correctness (S2) | 010, 025, 033 | S |
| 4 | Backend correctness | 004, 005, 006, 007, 012, 013, 014, 017, 019, 050 + backend P3 batch | M-L |
| 5 | Frontend lifecycle and polish (S3) | 011 + frontend P3 batch | M |
| Open decisions | needs a call before building | 020, 045, 052, 055 | see below |

---

## Phase 1: XSS and output encoding (S1)

**Root cause.** `esc()` (`app.js:560` and clones, aliases `escHtml`/`escapeHtml`/`_esc`) is the text-node serializer: it never escapes `"` or `'`. Every `attribute="${esc(x)}"` with attacker-influenced input is an attribute-breakout XSS. Quote-safe helpers `attr()`/`escAttr()`/`escapeAttr()` already exist and are correct.

**Locked strategy (from the adjudication).** Do NOT patch `esc()` globally; that regresses its legitimate text-node callers. Instead:

1. **Consolidate the helpers.** New shared module `frontend/js/lib/html.js` exporting `esc(s)` (text nodes), `attr(s)` (attribute values, escapes `& < > " '`), and `fmtMd(s)` (escape-then-format markdown-lite, see BUG-002). Views import from it; the per-view clones are deleted. Zero build step, plain ES module, matches existing conventions. Note there are THREE contexts, not two: `jsStr()` (`app.js:563`) covers JS-string-in-attribute (`onclick="f('${jsStr(x)}')"`); do not conflate it with `attr()` during the sweep. Phase 1 mostly eliminates that third context anyway (BUG-060's delegated-listener rewrite), but any surviving `jsStr()` site stays `jsStr()` wrapped in `attr()`.
2. **Route every attribute-context interpolation through `attr()`**, site by site, using the BUG-020 site table as the worklist.
3. **Re-escape at every `dataset`/`getAttribute` read** that feeds `innerHTML` (the BUG-001 decode-on-read trap): reading an escaped attribute yields the RAW string again, so the read side must escape too.

**Per-bug fixes.**

- **BUG-001** `dashboard.js:885,901-902`: write `data-name` with `attr()`, and escape `block.dataset.name` with `esc()` before the `tooltip.innerHTML` interpolation. Both ends.
- **BUG-002** `workflows.js:269-278`: replace the pipeline with `fmtMd(resp.response)`: escape the FULL text first, then run the `**`/`` ` ``/link regexes over the escaped text (the `errors.js:273` pattern). Port `errors.js` to the same shared `fmtMd()` so the two cannot drift again.
- **BUG-003** `player.js:37-38,264-266`: tighten the Apple Music slug capture to `([A-Za-z0-9._-]+)` and pass `embedUrl` through `attr()` at the iframe interpolation. Both layers; the regex alone is not a guarantee.
- **BUG-020 sites** (from the log's site table): `assistant.js:932` markdown-link href (also require `http(s):` scheme on LLM-supplied hrefs), `containers.js` label/name/url, `agent-fleet.js` id/label/HITL choice, `app.js:602` instance name, `fleet-health.js:58`, `dashboard.js:614,1102`, `connect-n8n-guide.js`, `secrets.js`, `music.js`. Mechanical `attr()` routing.
- **BUG-060** `workflows.js:274`, `errors.js:281`: replace the inline `onclick="copy(...)"` string-building with a delegated click listener reading the payload from a `data-*` attribute written via `attr()`. Kills the quoting problem structurally.
- **BUG-062** `admin.js:306,317`: `infoRow` escapes its value with `esc()`.

**Verification.**

- Promote the bug-hunt stub to `tests/stubs/stub_n8n.py` (from the scratchpad copy) and add a `--hostile` flag that names workflows/nodes/errors with attribute-breakout payloads (`x" onmouseover=... `, `<img src=x onerror=...>`, quote-heavy slugs).
- Playwright pass over Overview, Workflows, Executions/Errors, Containers, Secrets, Player against the hostile stub: zero dialogs, zero console CSP/eval errors, payloads render as literal text. Screenshot evidence per view.
- Grep gate: no remaining `="${esc(` / `='${esc(` / `escHtml(` in attribute position; document the one-liner in the PR.

---

### Phase 1 outcome (2026-08-16, verified)

Built and passed the hostile-stub gate. Findings for later phases:

- **A helper-consolidation sweep leaves duplicate-declaration landmines.** Two views (`workflows.js`, `settings.js`) kept a local `esc`/`attr` definition alongside the new import, a hard `SyntaxError` that takes the whole module offline (browser console: `Identifier 'esc' has already been declared`). The per-file `node --check` gate does NOT catch it (it parses each file as a loose script). For every later phase that adds imports, add a gate that greps each importing file for a surviving local definition of the same name.
- **The grep gate proves sinks were touched, not that all sinks were found.** The first sweep missed ~40 attribute sinks across 14 files still on quote-blind escapers. The residual pass caught them by grepping every `(attr-name)="${(esc|escHtml|escapeHtml|_esc)(` pattern, not just the BUG-020 worklist. Treat the worklist as a starting point, not the boundary.
- **Escape-then-format double-escapes `&`.** `fmtMd()`/`inline()` take text already escaped by `esc()` (so `&` is `&amp;`), then feed captured URLs to `attr()`, yielding `&amp;amp;` and broken query strings. Unescape `&amp;`→`&` before `attr()` on any value pulled from already-escaped text.
- Live-verified inert: BUG-001 (33-hover dataset round-trip), BUG-002 (`fmtMd` unit proof), BUG-003 (slug rejected, no iframe), plus Overview/Workflows/Errors/Containers. Stub promoted to `tests/stubs/stub_n8n.py --hostile`.

## Phase 2: Backend security hardening

- **BUG-009** `n8n_proxy/router.py:166` `edit_instance`: run `assert_safe_probe_url` on the submitted URL always; when `url` or `api_key` changed, probe with `test_connection_with` before saving (the create/rotate pattern). A failed probe returns 400 and leaves the stored instance untouched. Also fixes the BUG-034 blank-field hazard in the same route: blank `url`/`name` in the PUT is a 400, not a save.
- **BUG-008** `auth/service.py:513` TOTP enroll: enrolling while 2FA is enabled requires step-up (current TOTP code or password) and must NOT flip `enabled` or clobber the active secret. Store the new secret as `pending_secret_enc`; activation swaps it in atomically. Abandoned enrollment leaves 2FA exactly as it was.
- **BUG-021** `admin/router.py:111-119`: refuse deleting the last admin (400 with explicit copy). This closes the `accounts_exist() == false` reopen of unauthenticated `/api/auth/setup` without touching the setup flow itself.
- **BUG-031** `main.py:398-408`: enforce the body cap on the stream, not the header: read `request.stream()` incrementally and abort with 413 once the cap is crossed. Applies to all token-optional ingest routes, chunked included.
- **BUG-032** `auth/router.py:166`: run the PBKDF2 verify against a fixed dummy hash when the account does not exist, and give `/forgot` a constant-shape response and timing. Standard enumeration-oracle fix.
- **BUG-015** `assistant/mcp_client.py:385-389,415-417`: naming-convention profiles may only AUTO-APPROVE tools they positively classify as reads; an unmatched name falls through to the approval card (fail closed), never to read-only. Server-supplied names stop being a bypass.
- **BUG-016** `mcp_client.execute_tool:424`: emit `audit.record` for every MCP tool execution (name, server, decision path, autorun flag), matching the built-in tool audit. The 0.5.0 CHANGELOG already promises this.
- **BUG-018** `dashboard_mcp/server.py:162-179`: gate `list_secrets_metadata` (and any other admin-surface mirror) on admin, matching `/api/admin/secrets`. MCP transport inherits HTTP-side role parity as the rule.
- **BUG-040** `providers.py:857,1109`: scrub tool-arg logging through the existing secret-redaction helper before the `json.dumps(...)[:100]`, or drop arg values to keys-only at INFO.
- **BUG-043** `agent_fleet/runner.py:441`: pass the key via the provider client parameter instead of `os.environ`; if the SDK forces env, set it in the subprocess env dict only, never the parent process.

**Verification.** New pytest cases per bug (edit-PUT SSRF rejection, TOTP pending-secret state machine, last-admin 409/400, chunked 413 via raw socket test, MCP fail-closed classification with decoy names, audit row on MCP execute, MCP secrets tool 403 at operator). Timing-oracle test asserts hash work happens on the miss path (call-count via monkeypatch, not wall clock).

### Phase 2 outcome (2026-08-17, verified)

Built and verified; full suite 549 green (534 + 15 new). Findings:

- **BUG-031 shipped a self-recursion landmine.** The chunked-body guard reassigned `request._receive = guarded_receive`, and `guarded_receive` awaited `request._receive()`, now itself, so every chunked request recursed until the stack blew, the opposite of the intended 413. Fixed by capturing `original_receive` before the wrap. Caught by a middleware unit test, not by reading; a chunked-body test is mandatory here, the happy path (Content-Length) never exercises the wrapper.
- **Eight of the ten findings were already correctly implemented** in the working tree before verification (BUG-008/009/015/016/018/021/032/040 plus BUG-034), several with prose comments describing the exact hunt concern. The implementation was sound; the gap was test coverage (5 of 10 bugs) and the one recursion bug. Added tests for BUG-008 (TOTP stage/activate/abandon state machine), BUG-018 (operator denied, fail-closed without context), BUG-031 (chunked 413).
- **BUG-043 is mitigated, not eliminated.** The `os.environ["ANTHROPIC_API_KEY"]` write is now scoped and restored in a `finally`, closing the permanent-exposure concern, but two concurrent PydanticAI runs with different keys still race on the shared global. Acceptable while pydantic-ai forces env resolution; a real fix needs per-run env isolation (subprocess or client-param), tracked for a later pass.
- **Verified-by-audit against current code:** the `audit.record` in `mcp_client.execute_tool` scrubs its fields via `audit.scrub` (line 98 of audit.py), so passing raw `arguments` does not re-leak what BUG-040 closed.

## Phase 3: Fleet TLS correctness (S2)

**Root cause.** Seven-plus paths call `_verify()` (active-instance resolution) while contacting a DIFFERENT instance. Correct helper `tls_verify_for_instance(inst)` exists (`backend/net.py:111`) and is already used correctly at `client.py:641` (`get_execution_raw_for`); that call is the in-file reference pattern for the fix.

**Fix (BUG-010).** Thread `tls_verify_for_instance(inst)` through every per-instance client construction. Fix order within the phase: the credential-plaintext direction FIRST (`promote.py:138` `_provision_credential`, `n8n_credentials/router.py:229,381` mirror/unlink), since with an active instance at `tls_verify:false` those POST decrypted secret values to a target with verification disabled; the remaining sites are misdiagnosis, not exposure.

- `promote.py:138` `_provision_credential` (secret plaintext)
- `n8n_credentials/router.py:229,381` mirror/unlink (secret plaintext)
- `promote.py:66` `_probe_instance`
- `client.py:373` `_instance_health`
- `client.py:1103` `export_all_workflows_for`
- `mappings.py:300` `fetch_live_schemas`
- **BUG-025** `instance_map.py:149` `_probe_instance`
- **BUG-033** `client.py:302` `test_connection_with`: `verify=None` falls back to the GLOBAL default (`tls_verify()`), not the active instance's override; callers that know the target pass it explicitly.

Then add a lint-style guard: module docstring note in `client.py` that `_verify()` is legal only under `use_instance()` or for genuinely active-instance calls, and a unit test that greps the seven fixed sites for `tls_verify_for_instance` so a regression fails loudly.

**Verification.** Pytest with two seeded instances, active `tls_verify=False`, target `tls_verify=True` (and inverted): monkeypatched `httpx.AsyncClient` captures the `verify` kwarg per call for health, backup fan-out, promote probe, provision, mirror, schema fetch. The plaintext-POST-with-verify-off direction is the must-pass case.

### Phase 3 outcome (2026-08-17, verified)

All eight sites landed as specified, credential-plaintext direction first, plus BUG-025 (probe had no `verify=` at all) and BUG-033 (`test_connection_with` `verify=None` now resolves to the global `tls_verify()`, not the active instance's override). `fetch_live_schemas` gained an optional `inst` kwarg; both callers pass the instance through, and the `None` default falls back to the global flag for any caller with no instance context. The implementation arrived clean; the only fixes needed in review were two dead `_verify` imports left behind in `promote.py` and `n8n_credentials/router.py` (ruff F401) and import-order autofixes. Suite: 549 -> 559, all green. Lint guard in `tests/test_phase3_tls.py` greps each fixed site for `tls_verify_for_instance` so a silent revert fails the suite. Note for auditors: LLM-provider, GitHub-installer, player, and `_runtime` localhost clients intentionally remain on the global flag or no `verify=`; they never contact an n8n instance and were out of S2 scope.

---

## Phase 4: Backend correctness

**P1/P2.**

- **BUG-005** `storage.py:212` + `backfill.py:219,333`: `trace_id_for_execution(execution_id, instance_id)`, SQL `WHERE execution_id = ? AND instance_id = ?`. All callers pass the instance. Regression test: two instances sharing execution id "123", backfilling B must not match A's trace.
- **BUG-004** `health.py`: initialize `raw = None` above the guarded fetch. One line; the `(raw or {})` at :374 then degrades cleanly and `set_health` at :397 persists the span-only results. Test: enrichment with a raising `get_execution_raw_by_instance` still writes `checked_at`.
- **BUG-006** `client.py:1073` `export_all_workflows`: paginate with the same cursor loop as `export_all_workflows_for` (or delegate to it with the active instance). Test against the stub's 300-workflow mode.
- **BUG-007** `observability.js:88,96,185`: decouple the rebuild panel from `status.enabled`. When the receiver is off, still fetch span/rebuildable counts and render the Rebuild action alongside the setup panel. This is the spec-§Phase-1 recovery UI finally reachable in the outage state it was built for.
- **BUG-012** `n8n_credentials/router.py:279`: create-then-delete. Create the replacement first, rebind the mirror record, delete the old credential last; a failed create leaves the working credential and the mirror record untouched.
- **BUG-013** `promote.py:128`: mirror reuse must match `credential_type` and re-run `_assert_provision_allowed`; a type mismatch falls through to fresh provisioning.
- **BUG-014** `global-error-handler.json`: two steps. (a) Live-test an Error Trigger firing on n8n-dev and capture the actual payload shape into `tests/fixtures/` (the adjudication's pending confirmation). (b) Make the Code node read `err.workflow || (err.execution || {}).workflow || {}`, tolerant of both shapes. Bump the handler version marker so reinstall-over-existing picks it up.
- **BUG-017** `agent_fleet/router.py:194-220`: make the single-flight claim atomic in the router (claim `_live_run_id`/pause token under a lock BEFORE spawning the task, release on task completion). Double-click and double-resume tests.
- **BUG-019** `notes/index.py:253-264`: wrap the FTS5 `MATCH` in try/except on `sqlite3.OperationalError` and fall back to a quoted-literal query (`'"' + query.replace('"','""') + '"'`). `:)` and `"foo` return results or empty, never 500.
- **BUG-050** `scheduler.py:131`: move `enabled_fn()`/`interval_fn()` inside the per-tick try/except. Test: a raising `interval_fn` skips the tick and the job survives.

**Backend P3 batch** (same PR, mechanical):

- **BUG-022** ingest precedence delete: resolve the mapped instance id to the same id-space backfill writes under before the delete (`ingest.py:184-188` + `instance_map.py:115-123`).
- **BUG-023** backfill TOCTOU: re-check `trace_has_real_spans` immediately before insert; a hit discards the synthesized batch.
- **BUG-024** surface `origin` through `get_trace` and render the "reconstructed" badge in the waterfall (closes the spec-§3 labeling gap).
- **BUG-026** page budget counts only in-window rows.
- **BUG-027** namespace the root span id (synthetic name key, not the literal node name).
- **BUG-028** skip an execution with no usable `startedAt`, count it under `errors` instead of storing `received_at=''`.
- **BUG-029** fetch `includeData` once per execution and pass the payload to cost and health enrichers.
- **BUG-030** align `AGD_OTEL_RETENTION_HOURS` default to the spec's 168.
- **BUG-034** `config.py:944` (remaining half after Phase 2): blank-keeps-existing applied consistently; document which fields are clearable and how.
- **BUG-035** `unlink_mirror` under `_MIRRORS_LOCK`.
- **BUG-036** key the AWS compound template `awsApi`.
- **BUG-037** rename the migrated key so `openAiApi` auto-detect matches, with a config migration for existing stores.
- **BUG-038** `client._get` propagates status errors distinctly from 404 so promote reports the real failure.
- **BUG-039** `get_active_instance()` and `get_active_instance_id()` resolve the stale-id fallback identically.
- **BUG-041** cap `/api/knowledge/search` `limit` (50).
- **BUG-042** deep-merge `config` on partial source PUT.
- **BUG-044** `delete_run` clears `_PAUSED` and the in-memory graph.
- **BUG-046** `recreate_bundle` releases `deploy_id` on every early return (`finally`).
- **BUG-047** validate `instance_id` against a strict slug regex before path construction.
- **BUG-048** serialize `notes.append` with the storage lock.
- **BUG-049** self-container guard fails CLOSED when the container id cannot be resolved.
- **BUG-051** `_fetch_executions` honors its `instance_id` argument.

---

## Phase 5: Frontend lifecycle and polish (S3)

- **BUG-011 / S3** `app.js` `navigate()`: before swapping views, `await`/fire the outgoing view module's exported `cleanup()` if present, in a try/catch so a bad cleanup cannot block navigation. Then fix the sites the contract now reaches: `dashboard.js:167` poll and `:852-861` window listeners (register once, remove in cleanup), `containers.js:300,384` poll and `:1148,1065,1592` EventSources (`.close()` before nulling, including in `onerror`), `agent-fleet.js:299-302` WS sub + reconcile timer. Verification: Playwright navigates Overview → Notes → Overview x3 and asserts (via a debug counter or network log) exactly one poll cycle and no stacked listeners.
- **BUG-053** `admin.js:173`: fix the pattern to be `v`-flag valid (escape the hyphen or place it last).
- **BUG-054** hide admin-only nav/forms for non-admin roles (server RBAC already correct; this is UI honesty).
- **BUG-056** Flat view resolves instance id → name through the same map Grouped uses.
- **BUG-057** theme cards subscribe to the theme-change event the sidebar dropdown emits.
- **BUG-058** `settings-modules.js:132-139`: read `r.name`/`r.ref` from the object shape; drop the `startsWith` on objects.
- **BUG-059** `codelab.js:1337`: replace `btoa` with a Unicode-safe encoder (`TextEncoder` → base64) so emoji/smart quotes keep the Apply button and approval card alive.
- **BUG-061** add `autocomplete="off"`/`new-password` on Rotate-key, Add-Secret, and other credential inputs.
- **BUG-063** `wizard.js:1131-1140`: bind the SecretField input to wizard state so Back/forward preserves the typed key.

---

## Open decisions (parked, not scheduled)

| Bug | Question | Recommendation |
| --- | --- | --- |
| BUG-020 (module-worker isolation) | Enforce declared capabilities in the `subprocess` tier, or document it as a trusted tier and steer capability enforcement to the container tier only? Real enforcement means a sandbox redesign (seccomp/job objects/network namespace), not a patch. | Document `subprocess` as trusted-code tier now (README + install-time warning when a module declares restrictive capabilities under subprocess isolation); scope real enforcement as its own spec. |
| BUG-045 (mcp 2.x shim) | Pin mcp<2.0 and drop the dual-SDK shim, or finish the 2.x path? Latent while pinned. | Pin explicitly with a comment; revisit when a 2.x feature is needed. |
| BUG-052 (errors/messages retention) | Retention policy values and whether `health_checks` gets written or dropped. | 90-day prune on both tables via the existing scheduler; drop the dead `health_checks` table in a migration. |
| BUG-055 (viewer role reads) | Should viewer see read-only workflows/instances, or keep the 403 with a proper empty state? Product call. | Read-only GETs for viewer; it is the role's point. |

---

## Cross-cutting verification

- Full pytest suite green per phase (534 at baseline; each phase adds its regression cases).
- Ruff clean, `py_compile` on touched backend files before any 3066 deploy.
- Per-phase dogfood on 3066 (`compose -p agd-otel ... up -d --build dashboard`), minimum one day of soak before the next phase merges; Observe, Errors, and Backups are the canary views.
- The hostile-stub Playwright pass (Phase 1) re-runs after Phase 5, since S3's cleanup contract touches every view.
- Each phase's PR description maps commits to bug ids; the hunt log gains a `Fixed in` column per bug as phases land, keeping the log the single source of truth for status.

## Non-goals

- No fixes inside n8n itself; everything lands dashboard-side (the error-handler JSON ships FROM the dashboard, so it is in scope).
- No sandbox redesign for BUG-020 in these phases (see Open decisions).
- The stale `data/modules/agent-fleet/` dir on the 3066 volume is an ops task (remove the dir), not part of any phase.
