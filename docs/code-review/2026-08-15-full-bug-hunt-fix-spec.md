# Fix spec, 2026-08-15 full bug hunt

> **SUPERSEDED 2026-08-16.** Canonical remediation spec: [docs/specs/2026-08-16-bug-hunt-remediation.md](../specs/2026-08-16-bug-hunt-remediation.md). This document was compared and merged into it: its `client.py:641` reference, intra-S2 fix ordering, and `jsStr()` third-context note were adopted; it omits BUG-019/041/042 and leaves BUG-022..030 as an unfixed batch, which the canonical spec covers individually. Kept for the record; do not build from this document.

Technical specification for fixing the findings in [2026-08-15-full-bug-hunt.md](2026-08-15-full-bug-hunt.md), reconciled with the adjudication in [2026-08-15-full-bug-hunt-adversarial-review.md](2026-08-15-full-bug-hunt-adversarial-review.md).

**Scope policy.** This spec is fix-only, no new features. Where the hunt and the adversarial review disagreed, the adjudication in the review doc wins. Grades below are the reconciled grades.

**Sequencing.** Fix the three systemic root causes (S1, S2, S3) first. Each closes many individual sites at the root and several P1/P2s collapse into them. Then the remaining P1s, then P2s, then P3s grouped by module.

---

## Part 1. Systemic root causes

### FIX-S1. Escaping: route attribute contexts to a quote-safe escaper

**Root cause.** `esc()` (`frontend/js/app.js:560`) round-trips through the HTML text-node serializer, which encodes `&`, `<`, `>` but never `"` or `'`. Every `attribute="${esc(x)}"` with attacker-influenced `x` is an attribute-breakout XSS. Aliases `escHtml()`, `escapeHtml()`, `_esc()` share the flaw.

**What NOT to do.** Do not patch `esc()` to also escape quotes. `esc()` is correct for text-node contexts (element content, `<span>`, `<div>`), where quotes are harmless. A global quote-escaping patch regresses every legitimate text-node caller and still would not fix `data-*` round-trip re-decoding (BUG-001).

**Fix.**

1. Introduce (or confirm) a single attribute-safe helper, e.g. `attr(s)`, that escapes `&`, `<`, `>`, `"`, `'`. There is already `jsStr()` at `app.js:563` for JS-string-in-attribute contexts; the attribute-HTML helper is distinct from that. Audit whether `attr()` / `escAttr()` / `escapeAttr()` already exist somewhere in the frontend and converge on one name.

2. Sweep every template literal that interpolates into an HTML attribute (`title=`, `alt=`, `value=`, `data-*=`, `href=`, `src=`, `placeholder=`, `aria-*=`). Replace `esc(x)` with `attr(x)` at attribute sinks. Leave `esc(x)` at text-node sinks. Known sink clusters from the hunt:
   - `dashboard.js` (timeline `data-name`, instance name at `app.js:602`, unescaped n8n ids at `dashboard.js:1102`)
   - `containers.js` (label/name/url via quote-blind `escHtml()`)
   - `agent-fleet.js` (id/label/HITL choice)
   - `fleet-health.js:58`, `connect-n8n-guide.js` (instance URLs)
   - `secrets.js` (secret name, instance id)
   - `assistant.js:932` (markdown-link href from LLM output)
   - `music.js` (self-set config, self-only)

3. BUG-001 specifically: the `data-name` write AND the `block.dataset.name` read both need handling. The `dataset` read re-decodes entities, so re-escape (or use `textContent` on a scratch node) before interpolating into `tooltip.innerHTML`. Do not rely on fixing only the attribute write.

**Closes.** BUG-001, BUG-020, BUG-020-sites, the attribute-XSS portions of BUG-053-adjacent findings.

**Tests.** For each touched view: a stored value containing `"` and `'` and `<img>` rendered into both an attribute and a text node, asserted inert. At minimum, a DOM-level test on the timeline tooltip (BUG-001) and one attribute-sink per module cluster.

---

### FIX-S2. TLS: verify against the target instance, not the active one

**Root cause.** At least seven call paths build an `httpx.AsyncClient` with `_verify()` (the **active** instance's setting, `client.py:60-76`) while contacting a **different** instance. The correct helper `tls_verify_for_instance(inst)` exists (`backend/net.py`, imported at `client.py:12`) and is already used correctly at `client.py:641`.

**Fix.** Thread `tls_verify_for_instance(inst)` through every per-instance call site. Confirmed sites from the hunt and from a `_verify()` sweep:

| Site | File:line | Calls |
|---|---|---|
| `_instance_health` | `client.py:373` | fleet health per-instance |
| `export_all_workflows_for` | `client.py:1103` | scheduled backup fan-out (also the function the adversarial review initially vouched for; it carries this defect) |
| `_probe_instance` | `promote.py:66` | promote preflight |
| `_provision_credential` | `promote.py:138` | credential provision, POSTs decrypted secret plaintext |
| mirror / unlink | `n8n_credentials/router.py:229,381` | credential mirror |
| `fetch_live_schemas` | `mappings.py:300` | schema fetch |
| `_probe_instance` (observability) | `instance_map.py:149` | exporter-hash pinning (BUG-025) |
| `test_connection_with` fallback | `client.py:302` | rotate-key (BUG-033) |

The credential-provision and mirror paths are the dangerous direction: with the active instance at `tls_verify:false`, they POST decrypted secret plaintext to a target with verification disabled. Prioritize those.

**Closes.** BUG-010 (all sub-sites), BUG-025, BUG-033.

**Tests.** Two stub instances, one self-signed (`tls_verify:false`), one strict. Assert: fleet health reports each correctly; promote preflight to the self-signed target succeeds; a fan-out from a `tls_verify:false` active instance to a strict target still verifies.

---

### FIX-S3. Router: call view cleanup on navigation

**Root cause.** `app.js navigate()` swaps views without invoking any view's exported `cleanup()`. Views that start `setInterval`, `EventSource`, or WS subscriptions in `render()` and rely on `cleanup()` leak them on navigation and stack a fresh one on return.

**Fix.**

1. In `navigate()`: before swapping, look up the outgoing view module and, if it exports `cleanup()`, call it (wrapped in try/catch so one bad cleanup cannot break navigation).

2. Audit every view for teardown that belongs in `cleanup()` and add missing implementations:
   - `dashboard.js:167` (20s six-endpoint poll) and `dashboard.js:852-861` (window mouse/resize listeners re-added per widget-grid rebuild, never removed)
   - `containers.js:300,384` (15s poll)
   - `containers.js:1148,1065,1592` (log/deploy `EventSource.onerror` nulls the handle without `.close()`, so the browser keeps reconnecting an unclosable stream; call `.close()` in the error handler AND in `cleanup()`)
   - `agent-fleet.js:299-302` (WS sub + 4s reconcile)

3. The EventSource bug is a distinct defect from the missing-cleanup bug: even with cleanup wired, `onerror` must `.close()` before nulling, otherwise an error while the view is open leaks a reconnecting stream. Fix both.

**Closes.** BUG-011.

**Tests.** Navigate dashboard → containers → dashboard, assert only one poll interval is live and one EventSource is open. Trigger an EventSource error with the view open, assert the stream is closed.

---

## Part 2. Remaining P1s

### FIX-BUG-002. Workflow analyzer: sanitize LLM output before innerHTML

`frontend/js/views/workflows.js:269-278`. `__analyzeExec` feeds `resp.response` through markdown regexes into `resultEl.innerHTML`. The markdown regexes are themselves the injection vector: `.replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')` carries the unescaped `$1` group into an HTML context.

**Fix is a sanitizer, not esc().** A naive esc()-first breaks the intended `<strong>` / `<code>` formatting. Either:
- escape the raw response first, THEN run the markdown regexes over escaped text (so `$1` is already inert), or
- replace the regex pipeline with a real sanitizer (DOMPurify or equivalent) after formatting.

The first option is smaller and matches the sibling `errors.js:273` posture. Confirm `errors.js` does escape-first and mirror it exactly. Note the inline `onclick` Copy handler at `workflows.js:274` is separately broken (BUG-060) and its `JSON.stringify` double-quotes close the attribute; fix that in the same pass.

**Closes.** BUG-002. **Tests.** A prompt-injected reply containing `<img src=x onerror=...>` rendered inert; bold/code formatting still works.

### FIX-BUG-003. Player: constrain the Apple Music slug regex and escape the iframe src

`frontend/js/components/player.js:264-266` + regex at `:37-38`. The Apple Music matcher captures the slug as `([^/]+)`, permitting `"`, `<`, `>`; `embedUrl` lands in `<iframe src="${embedUrl}">` unescaped.

**Fix.** Two layers, both cheap: tighten the slug capture to exclude quote/angle chars (e.g. `([^/"<>]+)`), AND escape `embedUrl` at the iframe interpolation (attribute context, so the FIX-S1 attribute helper). The regex alone is not sufficient; defense in depth on a live-markup sink.

**Closes.** BUG-003. **Tests.** A saved/queued URL containing `"><img src=x onerror=alert(1)>` renders inert.

### FIX-BUG-005. Observability: scope the trace lookup by instance

`backend/modules/observability/backfill.py:219,333` + `storage.trace_id_for_execution` (`storage.py:212`, `WHERE execution_id = ?`, no instance filter). n8n execution ids are small integers that collide across instances, so backfilling instance B's execution "123" can match instance A's real trace.

**Fix.** Add an `instance_id` predicate to `trace_id_for_execution` (and any sibling execution-keyed lookups) and pass the owning instance through from both the precedence check (`backfill.py:219`) and the preview path (`backfill.py:333`). This is the exact recovery scenario the feature exists for; keep it P1.

**Closes.** BUG-005. **Tests.** Two instances with colliding execution ids; backfill on B rebuilds B's trace and does not count it `skipped_traced`; preview reports `already_traced` correctly per-instance.

---

## Part 3. P2s (grouped by module, in fix order)

**Auth / users**
- **BUG-008** TOTP re-enroll: guard `POST /api/auth/totp/enroll` so an already-enrolled caller must confirm with password + current code before `enabled=False` / secret overwrite. Prevents both the silent downgrade and the session step-up bypass. `auth/service.py:513`.
- **BUG-021** last-user delete: refuse deleting the final account (or the final admin) at `admin/router.py:111-119`; the current self-delete drops `accounts_exist()` to false and reopens unauthenticated `/api/auth/setup`. Keep P2.
- **BUG-032** login/forgot timing oracle: run a dummy PBKDF2 of equal cost when the account does not exist so the timing does not reveal account existence. `auth/router.py:166`.
- **BUG-009** edit_instance SSRF: add `assert_safe_probe_url` and a connection test to the instance-edit PUT, matching create / test-creds / rotate-key. `n8n_proxy/router.py:166`. Reconciled P2; note the adversarial review's caveat that the impact is more than consistency (background jobs transmit the stored key to the repointed host).

**Proxy / promote / credentials**
- **BUG-012** credential mirror: create the replacement credential BEFORE deleting the old one; only delete after the create succeeds. `n8n_credentials/router.py:279`.
- **BUG-013** promote reuse: when reusing a prior mirror, verify `credential_type` matches the request and re-run `_assert_provision_allowed`. `promote.py:128`.
- **BUG-006** manual export truncation (reconciled P2): add the cursor pagination loop to `export_all_workflows` (`client.py:1073`) mirroring `export_all_workflows_for:1084`. Backups are unaffected (they use the paginated path); this fixes only the manual export/import UI.
- **BUG-034 / BUG-035 / BUG-036 / BUG-037 / BUG-038 / BUG-039**: instance update blank-keep semantics (`config.py:944`), `unlink_mirror` read-modify-write outside `_MIRRORS_LOCK` (`n8n_credentials/router.py:393`), AWS `awsApi` vs `aws` key mismatch (`known_types.py:62` vs `mappings.py:105`), `OPEN_AI_KEY` naming vs `openAiApi` detect (`config.py:764`), `client._get` swallowing HTTP status to `{}` (`client.py:105` consumed at `promote.py:499`), and `get_active_instance()`/`get_active_instance_id()` stale-id disagreement (`config.py:314`).

**Observability**
- **BUG-004** (reconciled P2, corrected mechanism): `raw` is unbound on ANY fetch failure/timeout (`health.py:304/308`), not only the unknown-instance case, and the span-only loop's computed updates are discarded by the crash at `health.py:374`. Fix: initialize `raw = {}` before the guarded fetch so the dead-man's switch reads an empty dict instead of raising, and the span-only updates still reach `set_health` at `:397`.
- **BUG-007** Observe rebuild unreachable while receiver off: the view short-circuits on `!status.enabled` before checking span count, hiding the rebuild path in exactly the outage state it serves. Restructure `observability.js:88,96,185` so the "Rebuild traces" affordance is reachable whenever spans are absent, independent of receiver state. Backend backfill already works with the receiver off.
- **BUG-014** (verified against documented contract, live test pending): error-handler Code node reads `execution.workflow` but the documented Error Trigger shape emits `workflow` as a top-level sibling. Fix `global-error-handler.json` Extract Error Details to read `$json.workflow` (with the `execution.workflow` read kept as a fallback for shape drift). Confirm against a live Error Trigger before closing.
- **BUG-022 through BUG-030** (P3 cluster): ingest precedence delete keyed on mapped id, backfill TOCTOU, `get_trace` never returning `origin`, deep-window page budget, root-span id collision with a node named `workflow.execute`, empty `startedAt` → `received_at=''`, triple `includeData=true` fetch, retention default 72 vs spec 168. Fix as a batch in the observability module; several are one-liners. For BUG-030, decide whether the code default or the spec is authoritative and align them.

**Ingest**
- **BUG-031** chunked-body DoS: the request-size guard is Content-Length-only. Enforce the cap on the buffered body for `Transfer-Encoding: chunked` too (read with a size limit, reject over the cap) at `main.py:398-408` + the `/api/otel/v1/traces` receiver at `router.py:38`. Keep the verified P3 grade; single-request memory exhaustion on a token-optional endpoint.

**Assistant / MCP / fleet**
- **BUG-015** MCP classification fail-open: an `n8n-mcp` profile triggered by server-supplied names classifies any non-`n8n_`-prefixed tool as read-only. Make classification fail CLOSED for tools that do not match a known-write convention: unclassifiable must require approval, never auto-execute. `assistant/mcp_client.py:385-389,415-417`.
- **BUG-016** MCP autorun writes unaudited: add `audit.record` to `mcp_client.execute_tool` (`mcp_client.py:424`) so unattended MCP writes leave a forensic trail, matching the 0.5.0 CHANGELOG promise. `assistant/providers.py:84-86`.
- **BUG-017** agent-fleet single-flight race: `is_live()` is checked in the router but `_live_run_id` is set only inside the fire-and-forget task (`agent_fleet/router.py:194-220`). Make the guard atomic: set `_live_run_id` synchronously in the router (before `create_task`), and clear it in the task's finally. Same for the resume path. Measure the window; if `create_run` is awaited before the task spawns, the window is small but non-zero and worth closing regardless.
- **BUG-018** MCP `list_secrets_metadata` reachable at operator+: align the MCP mount's role gate with `admin/router.py:32` (admin-only), or scope the tool's output down to what an operator may see. `dashboard_mcp/server.py:162-179`.
- **BUG-040** tool-loop arg logging: scrub `json.dumps(args)[:100]` before INFO log so a secret passed as a tool argument does not reach the plain log. `assistant/providers.py:857,1109`.
- **BUG-043 / BUG-044 / BUG-045**: `_run_pydantic` persisting the resolved Anthropic key into `os.environ` (`agent_fleet/runner.py:441`), `delete_run` ignoring `_PAUSED` (`agent_fleet/router.py:240-247`), and the FastMCP-1.x `session_manager.run()` lifespan shim latent under mcp 2.0 (`main.py:66-70`).

**Docker / backups / notes**
- **BUG-046** `recreate_bundle` early returns never `release(deploy_id)`, leaking `_queues`. `docker_mgr/deployer.py:487-503`.
- **BUG-047** backup traversal guard resolves `base` from caller-supplied `instance_id`. `backups/service.py:247-258`.
- **BUG-048** `notes.append` un-serialized read-modify-write loses concurrent appends. `notes/storage.py:197-204`.
- **BUG-049** self-container destruction guard fails open when container id is unresolvable. `docker_mgr/client.py:265-316`.

**Scheduler / insights / errors**
- **BUG-050** (verified): `scheduler.py:131` calls `enabled_fn()`/`interval_fn()` outside the per-tick try/except, so one raise kills all scheduled jobs permanently. Move them inside the try.
- **BUG-051** `insights` `_fetch_executions` ignores its `instance_id` and queries the active instance. `insights/aggregator.py:67-99`. Latent (UI only sends `range`) but wrong.
- **BUG-052** `errors`/`messages` tables have no retention; `health_checks` created but never written. Decide a retention policy and either write or drop `health_checks`.

**Frontend misc (P2/P3)**
- **BUG-053** admin add-user `pattern="[a-zA-Z0-9_-]+"` throws under Chromium's `v`-flag regex. `admin.js:173`.
- **BUG-054 / BUG-055** viewer role: hide admin-only UI client-side (defense in depth; server already 403s writes), and either grant read on `/api/n8n/instances` + `/workflows` for viewer or render a permission empty-state instead of a raw "Insufficient role."
- **BUG-056** Executions/Errors Flat view labels instance "unknown" while Grouped resolves the same id to a name; share the resolver.
- **BUG-057** theme cards don't reflect sidebar-dropdown theme changes.
- **BUG-058** `settings-modules.js:132-139` calls `r.startsWith('$')` on `{name,ref,hint}` objects; every declared secret shows a false "missing" ✗.
- **BUG-059** `codelab.js:1337` `btoa(codeMatch[1])` throws on chars > 255, dropping the Apply button and approval card.
- **BUG-060** Copy button inline `onclick` broken by `JSON.stringify` double quotes (fix alongside BUG-002).
- **BUG-061** missing `autocomplete` / form containment causes browser autofill of owner credentials into Rotate-key and Add-Secret fields.
- **BUG-062** `admin.js:306,317` `infoRow('n8n URL', ...)` interpolates into `<span>` raw (self-XSS, admin-only).
- **BUG-063** onboarding n8n API-key SecretField has no input→state binding; a key typed last is lost on Back-then-forward. `wizard.js:1131-1140`.

**Module worker**
- **BUG-020** subprocess isolation does not enforce declared capabilities. The `subprocess` tier is a plain `Popen` with full host network/filesystem; `capabilities.*` feed only the AST scanner and bridge-token scope. Decide: either enforce capabilities in the subprocess tier (real sandboxing work) or document loudly that `subprocess` isolation is process-separation only, not a security boundary, and steer operators who need enforcement to the container tier. `agd_module_worker/sandbox.py` + `backend/modules/_runtime/supervisor.py:118-137`.

---

## Part 4. Non-code

- **Stale `agent-fleet` community dir.** Remove the leftover `data/modules/agent-fleet/` (hyphen) directory from the dogfood volume; the built-in `agent_fleet` (underscore) manifest is valid and the startup warning is not a code bug.

---

## Fix order recommendation

1. **FIX-S1** (escaping sweep) — closes the three stored-XSS P1s and ~a dozen P2/P3 sites at the root.
2. **FIX-S2** (TLS threading) — closes BUG-010/025/033; the credential-plaintext direction is the dangerous one.
3. **FIX-S3** (router cleanup) — closes the leak class.
4. **FIX-BUG-005** (instance-scoped trace lookup) — the one true P1 data-integrity hole outside the systemics.
5. **FIX-BUG-002 / FIX-BUG-003** — remaining XSS P1s (002 folds into the S1 sweep's sanitizer decision).
6. Auth cluster (BUG-008/021/032/009) — small, high-value.
7. Proxy/promote/credentials cluster, observability cluster, then the long tail grouped above.

## Cross-cutting cautions

- Do not patch `esc()` globally (see FIX-S1).
- BUG-002's fix is a sanitizer, not `esc()`; a naive esc-first breaks formatting.
- BUG-006's data-loss framing was corrected; backups were never affected. Fix only the manual export path.
- BUG-014 needs a live n8n Error Trigger test to confirm the contract before closing.
- BUG-004's trigger is any fetch failure, not only the unknown-instance case; initialize `raw` rather than special-casing.
