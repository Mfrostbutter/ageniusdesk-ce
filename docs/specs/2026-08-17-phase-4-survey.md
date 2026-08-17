# Phase 4 survey + resume notes (2026-08-17)

**Status: IMPLEMENTED 2026-08-17 in `8fc033d`** (all 31 bugs; see the Phase 4
outcome note in the remediation spec for deviations). Originally the pickup doc
for Phase 4 of
[2026-08-16-bug-hunt-remediation.md](2026-08-16-bug-hunt-remediation.md). Suite
was green at 559 tests entering the phase. Every site below was read on
2026-08-17; line numbers are from that read.

Spec scope: 10 P1/P2 fixes + P3 batch (BUG-022..030, 034..039, 041, 042, 044,
046..049, 051). Sequencing rules unchanged: full pytest green per phase,
dogfood on 3066 with a >=1 day soak between phases, hunt log gains a
"Fixed in" column per bug.

## P1/P2 fixes

### BUG-005, instance-scoped trace lookup (P1, do first)
- `storage.py:212` `trace_id_for_execution(execution_id)` — SQL has no
  `instance_id` predicate. Change signature to
  `trace_id_for_execution(execution_id, instance_id)`,
  `WHERE execution_id = ? AND instance_id = ?`.
- Callers to thread the instance through (4 total):
  - `backfill.py:219` (`_backfill_one`, has `instance_id` in scope)
  - `backfill.py:333` (`backfill_instance` page walk, has `instance_id`)
  - `router.py:126` (`trace_by_execution` — resolve active instance id;
    consider accepting an `instance_id` query param, default active)
  - `router.py:190` (`backfill_preview`, has `iid` in scope)
- Existing tests that call/monkeypatch it must be updated:
  `tests/test_backfill_api.py:134` (fake), `tests/test_trace_backfill.py:247`,
  `tests/test_trace_backfill_paths.py:438`.
- Regression test: two instances sharing execution id "123"; backfilling B
  must not match A's trace; preview `already_traced` correct per-instance.

### BUG-004, health.py unbound `raw`
- `health.py` `enrich_trace_health` (~:279): `raw` is assigned only inside the
  guarded fetch (~:304) but read at the dead-man's switch (`wf_data = (raw or
  {}).get(...)` ~:374). Any fetch failure -> UnboundLocalError, discarding the
  span-only updates before `set_health` (~:397).
- Fix: initialize `raw = {}` above the try (spec text says `raw = None`; the
  `(raw or {})` read degrades either way — `{}` matches the fetch's empty
  contract).
- Test: raising `get_execution_raw_by_instance` still writes `checked_at`.

### BUG-006, export_all_workflows pagination
- `client.py:1084` `export_all_workflows` (active-instance, single page,
  limit 250). Fix: paginate with the same cursor loop as
  `export_all_workflows_for` (:1103), or delegate to it with the active
  instance. Backups unaffected (they already use the `_for` path); this is the
  manual export/import UI only.
- Test against the stub's 300-workflow mode.

### BUG-007, observability.js rebuild decoupling
- `observability.js` `load()`: receiver-off path (:88-ish) renders
  `setupHtml()` and returns with NO backfill panel; the receiver-on/no-spans
  path (:96) renders `setupHtml(status)` + `bindBackfill()`. The recovery UI is
  unreachable in exactly the outage state it serves.
- Fix: when `!status.enabled`, still render the Rebuild action alongside the
  setup panel (fetch span/rebuildable counts; `setupHtml()` no-arg branch +
  `backfillPanelHtml(false)` + `bindBackfill()`). Backend backfill already
  works with the receiver off.

### BUG-012, credential mirror create-then-delete
- `n8n_credentials/router.py` `mirror_to_instance` (~:279): currently DELETEs
  the prior credential BEFORE POSTing the replacement. A failed POST leaves
  nothing. Fix: create first, `_record_mirror` the new entry, delete the old
  credential last (best-effort). Failed create leaves the working credential
  and mirror record untouched.

### BUG-013, promote mirror reuse checks
- `promote.py` `_provision_credential` (~:128): `prior` mirror is reused with
  no validation. Fix: reuse only when `prior.get("credential_type") ==
  cred_type` AND `_assert_provision_allowed(secret_name, tid, url)` passes;
  otherwise fall through to fresh provisioning.
- Note: `_assert_provision_allowed` currently runs after the reuse check; it
  must run before reuse too.

### BUG-014, error-handler payload shape
- `backend/n8n_workflows/global-error-handler.json`, Extract Error Details
  Code node: reads `err.execution.workflow`; documented Error Trigger shape
  emits `workflow` top-level.
- Two steps: (a) live-test an Error Trigger on n8n-dev, capture the actual
  payload into `tests/fixtures/`; (b) make the node read
  `err.workflow || (err.execution || {}).workflow || {}` (tolerant of both).
- "Bump the version marker": the shipped JSON has NO version field today.
  Install idempotency is name-match only (`errors/router.py` `_find_handler`).
  Add e.g. `"versionId"`/a `version` field or a static-tag marker so
  reinstall-over-existing can detect a stale handler.

### BUG-017, agent-fleet atomic single-flight
- `agent_fleet/router.py`: `start_triage` (~:194) and `resume_run` (~:215)
  check `runner.is_live()` then `asyncio.create_task(runner.run/resume)`;
  `_live_run_id` is set inside the task (`runner.py:508`, `:697`) — a real
  race window.
- Fix: claim synchronously in the router before `create_task`
  (e.g. `runner.claim(run_id)` under an `asyncio.Lock` in runner.py, returning
  bool), and keep the `finally: _live_run_id = None` release in the tasks.
  Same for resume. Double-click + double-resume tests.
- NOTE: `runner.run` sets `_live_run_id` at its top today; after the fix the
  claim moves to the router and `run`/`resume` should assert/not re-claim.

### BUG-019, notes FTS5 fallback
- `notes/index.py:132` `search()`: FTS5 `MATCH ?` at :146 with
  `_sanitize_query` (:253). Pass-through branch (quoted phrases / explicit
  operators) can still raise `sqlite3.OperationalError` -> 500.
- Fix: wrap the MATCH `db.execute` in try/except `sqlite3.OperationalError`
  and retry with a quoted literal: `'"' + query.replace('"', '""') + '"'`.
  `:)` and `"foo` must return results or empty, never 500.

### BUG-050, scheduler per-tick guard
- `scheduler.py:131` `_run()`: `job.enabled_fn()` and `job.interval_fn()` are
  called OUTSIDE any try (in the tick loop and in `_fire`'s reschedule at
  :159). One raise kills the whole scheduler loop permanently.
- Fix: wrap per-job evaluation in try/except (log + continue); also the
  `interval_fn()` call in `_fire`'s finally. Test: a raising `interval_fn`
  skips the tick and the job survives.

## P3 batch, observability (BUG-022..030)

- **BUG-022** `ingest.py:184-188`: precedence delete uses the mapped
  instance_id from `parse_request`; a batch stamped `unknown-<hash>` deletes
  nothing if backfill wrote under the real id (learn step re-attributes later
  via `instance_map.py:217-220` UPDATE). Fix: resolve through
  `instance_map` (pins) to the id-space backfill writes under before the
  delete. Files: `ingest.py:184-188`, `instance_map.py:115-123`.
- **BUG-023** `backfill.py` `_backfill_one` (:212): TOCTOU between the
  precedence check (:219) and `insert_spans` (:232). Fix: re-check
  `trace_has_real_spans` immediately before insert; a hit discards the
  synthesized batch.
- **BUG-024** `storage.get_trace` (:259) never returns `origin`, though
  `insert_spans` stores it. Fix: add `origin` to the span dict, then render a
  "reconstructed" badge in `frontend/js/components/trace-waterfall.js`
  (`buildWaterfall` :48, header block :60-67 is the natural slot).
- **BUG-026** `backfill.py` page walk: `summary["scanned"]` increments BEFORE
  the retention-floor check, so outside-window rows eat the page budget
  (:318-330). Fix: count only in-window rows against the cap.
- **BUG-027** `backfill.py:40` `_span_id(trace_id, "workflow.execute", 0)` —
  a real node NAMED `workflow.execute` collides with the root span id. Fix:
  namespace the root key (e.g. `_span_id(trace_id, "\x00root", 0)` or a
  dedicated constant), not the literal node name.
- **BUG-028** `backfill.py` `synthesize()` (:97): empty `startedAt` yields
  `received_at = ""` and `start_ns = 0`. Fix: skip executions with no usable
  `startedAt`, count them under `errors` (in `_backfill_one` / page walk).
- **BUG-029** triple `includeData=true` fetch per execution:
  `backfill._backfill_one` fetches raw (:222), then `_enrich` ->
  `cost.enrich_trace` fetches again (`cost.py:77`), then
  `health.enrich_trace_health` fetches a third time (`health.py:305`).
  `ingest.py:216-218` fires cost+health per completed trace the same way
  (2 fetches), and `router.py:_enrich` (:104-112) on trace open.
  Fix: fetch `includeData` once per execution and pass the payload to both
  enrichers (optional `raw=` param on `enrich_trace` /
  `enrich_trace_health`; None preserves the self-fetch for the router/ingest
  lazy paths).
- **BUG-030** `config.py:142`: `agd_otel_retention_hours: int = 72` vs spec's
  168. Decision needed: align code default to 168 (spec authoritative).

## P3 batch, misc (BUG-034..051)

- **BUG-034** `config.py:929` `update_instance`: blank-keeps-existing exists
  for `api_key`, `owner_password`, `owner_email`, `login_url`. Apply
  consistently to remaining sensitive/string fields (audit: `name`, `url`,
  `color`, `tls_verify`) and document which fields are clearable + how (e.g.
  explicit null clears).
- **BUG-035** `n8n_credentials/router.py` `unlink_mirror` (:361-411): load ->
  n8n DELETE -> pop -> `_save_mirrors` runs OUTSIDE `_MIRRORS_LOCK`
  (`_record_mirror` at :89 is the only locked writer). Fix: wrap the
  read-modify-write under `_MIRRORS_LOCK`; re-read fresh state inside (mirror
  `_record_mirror`'s pattern). Do not hold the lock across the network DELETE
  (do n8n side first, then locked local mutation, or accept a retry).
- **BUG-036** AWS key mismatch: `known_types.py:62` lists n8n type `awsApi`,
  but `mappings.py:105` keys the compound override as `"aws"`. Fix: key it
  `awsApi` (check for any other consumers of the `"aws"` key first).
- **BUG-037** OpenAI key naming: migration prefixes (`config.py:758` +
  `providers.py:293`) mint `$OPEN_AI_KEY`, but `detect_type_from_name`
  (`known_types.py:24`) matches `OPENAI`, not `OPEN_AI`, so auto-detect to
  `openAiApi` misses. Fix: migrate existing `OPEN_AI_KEY` secrets to
  `OPENAI_API_KEY` (move entry in `load_secrets()`/`save_secrets()`, plus
  update references in config: `assistant.api_key` `$`-ref,
  `agent_fleet/runner.py:117,124` env+secret-name lists,
  `providers.py:293,399,585`, `config.py:758` prefix map).
- **BUG-038** `client.py:105` `_get` swallows HTTP status to `{}` on BOTH 404
  and other errors; promote (:499 `if not wf:`) reports "Not found on source"
  for a 500. Fix: propagate non-404 status distinctly (raise or a sentinel)
  so promote reports the real failure. Audit `_get` consumers for `{}`-
  sentinel reliance before changing.
- **BUG-039** `config.py:314` vs :326: `get_active_instance_id()` returns the
  raw persisted id with no validation; `get_active_instance()` falls back to
  the first instance on a stale id. The two disagree. Fix: identical
  resolution in both (stale id -> first instance's id).
- **BUG-041** `knowledge/router.py:115` `search`: `limit: int = 10`
  uncapped. Fix: `limit = max(1, min(int(limit), 50))`.
- **BUG-042** `knowledge/storage.py:65` `update_source`: `config` patch
  REPLACES `config_json` wholesale (:74-76). Fix: deep-merge `config` on
  partial source PUT (load existing, merge, write).
- **BUG-044** `agent_fleet/router.py` `delete_run` (:237): deletes the row but
  ignores `runner._PAUSED`, leaking the parked in-memory graph and leaving
  `is_paused()` true for a gone run. Fix: `runner._PAUSED.pop(run_id, None)`
  (via a runner helper, not direct dict access from the router).
- **BUG-046** `docker_mgr/deployer.py` `recreate_bundle` (:461): three early
  returns (bad bundle_id :487, missing snapshot :492, non-bundle template
  :508) `await q.put(None)` but never `release(deploy_id)`, leaking `_queues`
  entries. Fix: `try/finally: release(deploy_id)` around the body (note the
  happy path delegates to `deploy_bundle`, which releases :458 — do not
  double-release; check `deploy` :770 too).
- **BUG-047** `backups/service.py:247` `resolve_backup_path`: base resolves
  from caller-supplied `instance_id`; `_instance_dir` (:117) is
  `BACKUPS_DIR / instance_id` with no validation. Fix: validate
  `instance_id` against a strict slug regex before path construction.
- **BUG-048** `notes/storage.py:197` `append`: un-serialized read-modify-write
  (`read_text` -> `write`), so concurrent appends lose data. Fix: serialize
  with a storage `asyncio.Lock`.
- **BUG-049** `docker_mgr/router.py:37` `_guard_not_self`: on exception or
  unresolvable self-container (`client.py:304` `is_self_container` returns
  False), the guard fails OPEN. Fix: fail CLOSED when the container id cannot
  be resolved (refuse destructive action when we cannot prove not-self; keep
  the `/.dockerenv` absent / non-containerized case allowed — that gate is at
  `self_container`'s override/dockerenv check :278).
- **BUG-051** `insights/aggregator.py:67` `_fetch_executions`: takes
  `instance_id` but always calls `n8n_client.list_executions` (active
  instance); errors correctly use the requested instance
  (`_build_payload` :130-141). Fix: honor the argument — resolve the instance
  via `get_instance_by_id` and wrap the fetch in `use_instance(inst)` (the
  promote module's pattern), or use a per-instance direct call. Latent today
  because the UI only sends `range`, but the router
  (`insights/router.py:32`) already accepts `instance_id`.

## Resume checklist

1. Re-run baseline: `.\.venv\Scripts\python.exe -m pytest tests/ -q`
   (expect 559 green; venv python, system `python` is msys64 without pytest).
2. Fix order: BUG-005 first (only true P1 data-integrity hole), then 004, 006,
   012, 013, 017, 019, 050, 007; BUG-014's live Error Trigger capture can run
   any time (needs n8n-dev).
3. New tests in `tests/test_phase4_*.py` per bug; regression lint guards where
   a silent revert is plausible (mirrors Phase 3's `test_phase3_tls.py`
   pattern).
4. Full suite green, then dogfood on 3066 with >=1 day soak before Phase 5.
5. Hunt log gains a "Fixed in" column per bug closed.
