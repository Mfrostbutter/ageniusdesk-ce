# Code Review: Silent Failure Detection

**Date:** 2026-07-07
**Reviewer:** GitHub Copilot
**Scope:** Full review of the silent-failure detection feature (CHANGELOG [Unreleased] > ### Added)
**Spec:** `docs/specs/2026-07-07-silent-failure-detection.md`
**Architecture doc:** `docs/architecture/silent-failure-detection.md`
**Mode:** Review only. No code was changed.

---

## Executive Summary

The silent-failure detection feature catches n8n runs that report "success" but are actually broken: Continue-On-Fail demoted errors and low-output anomalies. The implementation is solid in its core detection logic, classifier design, and frontend surfacing. The test suite covers the happy paths well.

There are **2 major findings** (spec compliance gaps that could cause false positives or prevent runtime disabling), **7 minor findings** (missing fields, edge cases, test gaps), and **3 informational findings** (design observations, not defects).

The most impactful issue is the missing "Handled not silent" downstream-connection check (Finding 2). Without it, errors that flow out `main[1]` to a wired error handler will be falsely flagged as silent failures, producing noise and eroding trust in the detector.

---

## What Was Reviewed

### Backend
- `backend/modules/observability/health.py` (~400 lines) - core detector
- `backend/modules/observability/ingest.py` (~200 lines) - OTLP ingest + eager trigger
- `backend/modules/observability/storage.py` (~400 lines) - span persistence + health columns
- `backend/modules/observability/router.py` (~200 lines) - OTLP receiver routes + trace API
- `backend/database.py` (lines 140-200) - schema migration for health columns
- `backend/modules/errors/collector.py` (~300 lines) - error storage + WebSocket broadcast
- `backend/config.py` (lines 125-160) - env var definitions

### Frontend
- `frontend/js/components/error-item.js` (~110 lines) - shared error-item renderer with SILENT badge
- `frontend/js/views/dashboard.js` (lines 540-820) - Silent Failures stat card
- `frontend/js/views/observability.js` (lines 40-180) - trace list, metrics, waterfall
- `frontend/js/views/errors.js` - errors view grouping
- `frontend/js/views/insights.js` - analytics tile
- `frontend/js/views/fleet-health.js` - fleet errors tab
- `frontend/js/app.js` (lines 340-370) - global toast + badge bump
- `frontend/js/components/trace-waterfall.js` - health status coloring + health line

### Tests
- `tests/test_health_detection.py` (~400 lines, ~20 test cases)

### Docs
- `docs/specs/2026-07-07-silent-failure-detection.md` - feature spec
- `docs/architecture/silent-failure-detection.md` - architecture writeup

---

## Spec Compliance Summary

| Spec requirement | Status |
|---|---|
| Phase 0: confirm span shape | Done |
| Phase 1: MVP silent-error detector | Done |
| Phase 2: low-output anomaly + drop-origin suppression | Done |
| Phase 3: dead-man's switch | Not built (deferred, expected) |
| Phase 4: egress + self-healing | Not built (deferred, expected) |
| Eager trigger on OTLP ingest | Done |
| Fallback poller | **Not implemented** (Finding 5) |
| `on_error_mode` storage column | **Not created** (Finding 1) |
| "Handled not silent" `main[1]` check | **Not implemented** (Finding 2) |
| `AGD_HEALTH_ENABLED` master switch | **Not defined** (Finding 3) |
| `AGD_HEALTH_POLL_*` env vars | Not defined (Finding 4, deferred) |
| `AGD_HEALTH_ALERT_EGRESS` env var | Not defined (Finding 4, deferred) |
| Error type normalization (object vs string) | Done |
| Per-node baseline classification | Done |
| Drop-origin suppression | Done |
| `error_summary` sanitized + truncated | Truncated only (Finding 8) |
| `store_error` integration + live broadcast | Done |
| `otel:silent` WebSocket event | Done |
| SILENT badge in error-item component | Done |
| Silent Failures stat card on dashboard | Done |
| Silent failures metric in observability + insights | Done |
| Trace waterfall health coloring | Done |
| Idempotency guard (`has_health`) | Done |
| Test coverage for core paths | Done (gaps in Finding 11) |

---

## Findings

### Finding 1: `on_error_mode` column spec'd but not created
**Severity: MAJOR**

The spec lists `on_error_mode` as a storage column with values `continueRegularOutput` / `continueErrorOutput` / `stopWorkflow`. This column is meant to record how the node was configured to handle errors, which is critical context for distinguishing "Continue-On-Fail demoted error" from a genuinely silent failure.

**What's missing:**
- `backend/database.py` `_health_cols` list has 9 columns; `on_error_mode` is not among them. The ALTER TABLE migration never creates the column.
- `storage.py` `set_health()` does not write `on_error_mode`.
- `storage.py` `get_trace()` does not return `on_error_mode`.
- `health.py` `enrich_trace_health()` never reads or sets `on_error_mode`.

**Impact:** Without `on_error_mode`, the detector cannot record whether a node was configured with Continue-On-Fail or Continue-Error-Output. This means the "why" behind a demoted error is lost. The detector still catches the error (via `runData`), but the audit trail for operators is incomplete. An operator reviewing a flagged silent failure cannot see whether the node was explicitly set to continue on error (intentional) or whether the error was unexpected (true silent failure).

**Recommendation:** Add `on_error_mode TEXT` to `_health_cols`, write it in `set_health()`, return it in `get_trace()`, and populate it from `runData` during enrichment. Surface it in the trace waterfall health line.

---

### Finding 2: "Handled not silent" `main[1]` downstream-connection check not implemented
**Severity: MAJOR**

The spec defines a rule: if a node's error flows out `main[1]` (the error output) to a wired downstream error-handler node, the failure is **handled, not silent**. The detector should not flag it.

**What's missing:**
- `health.py` `_node_error_from_run(run)` unions three error sources (execution status, run-level `run.error`, item-level `json.error`) and returns the normalized error. It does **not** check whether the node has a `main[1]` connection wired to a downstream node.
- There is no logic that inspects the workflow's connection graph to determine if an error output is wired.

**Impact:** This is a false-positive risk. If an operator wires an error handler (e.g., a "Catch" node or a "Set Error Variable" node) to `main[1]`, and the upstream node errors but the workflow continues and finishes "success", the detector will flag it as a silent failure. But it's not silent: the error was routed to a handler. This produces noise and erodes trust in the detector. The spec explicitly calls this out as a suppression rule.

**Recommendation:** Before flagging a node as silent, check the workflow connection graph (available in `runData` or the workflow JSON) for a wired `main[1]` output on the erroring node. If wired, suppress the silent flag (or mark it as "handled" rather than "silent"). This may require fetching the workflow definition alongside `runData`.

---

### Finding 3: `AGD_HEALTH_ENABLED` master switch not defined
**Severity: MAJOR**

The spec lists `AGD_HEALTH_ENABLED` as the master switch for the health enrichment subsystem. It should allow operators to disable silent-failure detection without disabling OTLP ingestion entirely.

**What's missing:**
- `backend/config.py` does not define `agd_health_enabled` (or any `agd_health_*` field beyond the classifier tuning knobs).
- `health.py` `enrich_trace_health()` does not check a settings flag at entry.
- `ingest.py` `ingest_trace_request()` does not check before scheduling `asyncio.create_task(health.enrich_trace_health(tid))`.

**Impact:** There is no way to disable health enrichment without disabling OTLP entirely (`AGD_OTEL_ENABLED=false`). If the detector causes performance issues, false positives, or unwanted error-table pollution, the only remediation is to turn off all observability. Operators need a granular kill switch.

**Recommendation:** Add `agd_health_enabled: bool = True` to `config.py` Settings. Gate `enrich_trace_health()` at entry with an early return if disabled. Gate the `asyncio.create_task` call in `ingest.py` on the same flag.

---

### Finding 4: Poller and egress env vars not defined
**Severity: MINOR**

The spec lists `AGD_HEALTH_POLL_ENABLED`, `AGD_HEALTH_POLL_INTERVAL_SEC`, and `AGD_HEALTH_ALERT_EGRESS` as configuration. None are defined in `config.py`.

**Context:** The fallback poller (Phase 1) and egress/self-healing (Phase 4) are not yet built. These env vars are forward-looking spec entries.

**Impact:** Low. The features they configure don't exist yet. But if someone follows the spec to configure the system, they'll set env vars that are silently ignored.

**Recommendation:** Either define them with sensible defaults (poll disabled by default, interval 300s, egress empty) or add a note to the spec that they are reserved for future phases. Defining them now with `poll_enabled: bool = False` would be the cleaner path.

---

### Finding 5: Fallback poller not implemented
**Severity: MINOR**

The spec Phase 1 describes "eager trigger on OTLP ingest + fallback poller." Only the eager trigger exists. `ingest.py` schedules `asyncio.create_task(health.enrich_trace_health(tid))` for each trace with a `workflow.execute` root span. There is no background poller that retries enrichment for traces where the eager trigger failed.

**Impact:** If the eager enrichment task fails (e.g., `runData` fetch times out at 8s, the n8n instance is briefly unreachable, or an exception is swallowed by `asyncio.create_task`), the trace is never re-enriched. The `has_health` idempotency guard checks `checked_at IS NOT NULL`, but if the task errored before setting `checked_at`, the guard won't block a retry. However, there is no retry mechanism to invoke it. Traces that fail enrichment on first pass are permanently un-enriched.

**Recommendation:** Implement a lightweight periodic poller (e.g., every `AGD_HEALTH_POLL_INTERVAL_SEC` seconds) that queries for traces with `checked_at IS NULL` and a `workflow.execute` root span, then enriches them. This is spec'd for Phase 1 and should be included before the feature ships.

---

### Finding 6: `get_trace` missing fields
**Severity: MINOR**

`storage.py` `get_trace()` returns `health_status`, `error_type`, `error_summary`, `http_status`, and `output_items` per span. It does **not** return `input_items`, `node_id`, `silent`, or `on_error_mode`.

**Impact:**
- `input_items`: The trace waterfall can't show the input count, which is useful context for understanding a drop (e.g., "output 0 from input 50" vs "output 0 from input 0").
- `node_id`: The waterfall can't display the n8n node name alongside the span. The span name from OTLP may differ from the n8n node name.
- `silent`: The frontend can't directly check the `silent` flag from the trace response. It infers silence from `health_status` instead, which works but is indirect.
- `on_error_mode`: See Finding 1.

**Recommendation:** Add `input_items`, `node_id`, `silent`, and (once it exists) `on_error_mode` to the `get_trace()` SELECT and row dict. Update `trace-waterfall.js` to display them in the health line.

---

### Finding 7: Run-span alignment is a heuristic
**Severity: MINOR**

In `health.py` `enrich_trace_health()`, per-span run data is aligned by index:

```python
run = runs[i] if i < len(runs) else runs[-1]
```

where `i` is the span index within the `by_node` group and `runs` is the list of run entries from `runData` for that node.

**Impact:** If a node has multiple executions within one trace (uncommon but possible with loops or sub-workflows), or if the span count and run count diverge for any reason, the alignment breaks. The fallback `runs[-1]` assigns the last run to all excess spans, which is a guess. A misaligned run means the wrong error data is attributed to a span.

**Recommendation:** If n8n provides a correlation ID between runs and spans (e.g., via span attributes or run metadata), use it for alignment instead of positional index. If not, document the assumption and add a defensive log when `len(runs) != len(spans_for_node)`.

---

### Finding 8: `error_summary` truncation without sanitization
**Severity: MINOR**

The spec says `error_summary` should be "sanitized, truncated." `_normalize_error()` truncates to 500 characters but does no sanitization (no redaction of secrets, tokens, PII, or credentials).

**Impact:** n8n error messages can contain sensitive data: API keys in HTTP error responses, connection strings, user emails, OAuth tokens. These would be stored verbatim in the `error_summary` column, broadcast over WebSocket, and rendered in the frontend. The `error-item.js` component is XSS-safe (uses `esc()`), so there's no injection risk, but there is a data-exposure risk. Anyone with dashboard access sees the raw error message.

**Recommendation:** Add a sanitization pass before truncation. At minimum, redact common secret patterns (bearer tokens, API keys, password fields). Consider a regex-based scrubber for `Bearer ...`, `api_key=...`, `password=...`, and long hex/base64 strings that look like tokens.

---

### Finding 9: Potential double-broadcast for silent failures
**Severity: INFO**

When a silent failure is detected, `health.py` does two things:
1. Calls `store_error()` from `collector.py`, which INSERTs into the `errors` table and broadcasts an `"error"` WebSocket event.
2. Separately broadcasts an `"otel:silent"` WebSocket event.

The frontend handles both:
- `app.js` handles `"otel:silent"` with a toast notification ("Silent failure: X ran green but Y failed") and bumps the error badge.
- The `"error"` event from `store_error` likely triggers the errors list view to refresh and show the new error row.

**Impact:** This is not a bug. The two events serve different UI purposes: the toast is an immediate alert, and the error row is a persistent record. But it's worth noting that one logical event (a silent failure was detected) produces two WebSocket broadcasts. If the frontend ever deduplicates or counts events, this could cause double-counting. The current implementation doesn't appear to have this problem.

**Recommendation:** No action needed. Document the dual-broadcast as intentional so future maintainers understand the two events are related.

---

### Finding 10: `errors` table has no `silent` column
**Severity: INFO**

Silent failures are written to the `errors` table with `error_type = "Silent failure"` (the `SILENT_ERROR_TYPE` constant). There is no boolean `silent` column on the `errors` table. Distinguishing silent from loud errors requires a string comparison on `error_type`.

**Impact:** Low. The string constant is centralized in `health.py` and used consistently in `error-item.js` (`SILENT_TYPE`). But SQL queries that filter or aggregate silent failures must use `WHERE error_type = 'Silent failure'` rather than a boolean flag, which is slightly more fragile (typos, case sensitivity) and prevents indexing on a boolean.

**Recommendation:** Consider adding a `silent INTEGER DEFAULT 0` column to the `errors` table in a future migration. This would make filtering and aggregation cleaner. Low priority since the current approach works.

---

### Finding 11: Test coverage gaps
**Severity: MINOR**

The test suite (`tests/test_health_detection.py`, ~20 cases) covers the core detection paths well: error normalization (object vs string, item-level vs run-level), E2E silent detection under green runs, loud error non-flagging, classifier unit tests (cold start, steady producer, intermittent, magnitude drop, drop-origin, drop-victim), and errors pipeline integration.

**Not tested:**
- **Idempotency:** No test that `has_health()` prevents re-enrichment of an already-checked trace. The guard exists in code but is not verified.
- **Concurrent enrichment race:** No test that two concurrent `enrich_trace_health()` calls for the same trace don't produce duplicate `store_error` rows or duplicate broadcasts.
- **Run-span alignment edge cases:** No test for `len(runs) != len(spans_for_node)` or the `runs[-1]` fallback.
- **`error_summary` truncation at 500 chars:** No test that long error messages are truncated.
- **`_fix_mojibake` repair:** No test for the double-encoded UTF-8 repair logic in `ingest.py`.
- **`_map_instance` fallback:** No test for the instance attribution fallback to the active instance when no attribute matches.
- **Retention pruning interaction with health history:** No test that `node_output_history` and `node_input_history` behave correctly after `prune()` removes old spans.
- **`AGD_HEALTH_ENABLED` gating:** No test because the feature doesn't exist (Finding 3).

**Recommendation:** Add tests for idempotency (enrich same trace twice, assert one error row), concurrent enrichment (two tasks, assert one error row), and truncation (500+ char error message, assert truncated). The mojibake and instance-fallback tests are lower priority but would harden the ingest path.

---

### Finding 12: Cold start vs retention pruning interaction
**Severity: MINOR**

The classifier treats a node with fewer than `AGD_HEALTH_MIN_SAMPLES` (default 20) output history entries as a cold start, which never fires. This is correct for new nodes. But if retention pruning (`prune()` in `storage.py`) removes old spans, a steady producer's history could shrink below `min_samples`, causing reclassification as cold start.

**Scenario:** A node produces output reliably for 300 traces. `AGD_HEALTH_WINDOW` is 200, so `node_output_history` returns the last 200. Then `prune()` runs (age-based, 72h default, or row-cap at 500K). If the node hasn't run in 72h, all its spans are pruned. When it runs again, `node_output_history` returns 0 rows (all pruned), `n < min_samples`, cold start, never fires. If it then produces zero output (a real anomaly), the detector misses it.

**Impact:** This is a silent-recall regression. A previously-steady node that goes quiet long enough to be pruned, then returns with a zero-output anomaly, will not be flagged. The detector "forgets" the node was ever steady.

**Recommendation:** This is an inherent limitation of history-based detection with retention. Options: (1) persist a per-node summary (last seen, steady flag, sample count) that survives span pruning, so the classifier can restore "steady" status quickly after a gap; (2) lower the retention age or raise the row cap so history survives longer; (3) document this as a known limitation. Option 1 is the most robust but adds a table. Option 3 is acceptable for now.

---

## Architecture Observations

### Detection logic correctness
The two-detector approach is sound. Demoted errors (modes 1/2) are caught by fetching `runData` and unioning three error sources. Low-output anomalies (mode 3) are caught by per-node baseline classification with drop-origin suppression. The `is_silent` flag correctly requires `exec_status == "success"` AND `health_status in (ERROR, LOW)`, so loud errors are excluded.

### Classifier design
The precision-over-recall classification (cold start never fires, intermittent/poller zero is expected, dormant zero is norm, only steady producers fire) is well-reasoned. The drop-origin suppression (downstream pass-through victims suppressed, only origin fires) prevents alert storms when a single upstream node drops output.

### Schema migration
The idempotent `ALTER TABLE ADD COLUMN` pattern in `database.py` is correct and safe for re-runs. The `PRAGMA table_info` check before each ALTER prevents errors on already-migrated databases. The index `idx_otel_spans_node` on `(node_id, start_ns DESC)` supports the history queries efficiently.

### Eager trigger
The `asyncio.create_task` pattern in `ingest.py` is fire-and-forget, which is appropriate for eager enrichment. The 8s timeout on the `runData` fetch prevents the task from hanging indefinitely. However, fire-and-forget means exceptions are swallowed (no logging, no retry). See Finding 5.

### Errors pipeline integration
Writing silent failures to the `errors` table with a distinct `error_type` is a clean integration. The `SILENT_ERROR_TYPE` constant is centralized. The `error-item.js` component renders the SILENT badge consistently across all views that use it (dashboard, errors, fleet health).

### UI surfacing
The feature is surfaced in five places: dashboard stat card, observability metrics strip, trace waterfall health coloring, errors list (via SILENT badge), and global toast. The `otel:silent` WebSocket event drives the toast and badge bump. The `error` event from `store_error` drives the errors list refresh. The UX is coherent.

### Security
- The `runData` fetch uses the active n8n instance's API key from the encrypted secret store. No SSRF risk since the URL comes from the configured instance, not user input.
- The OTLP receiver is token-checked in `main.py` (not reviewed in detail here, but the pattern is established).
- `error_summary` sanitization is the main security gap (Finding 8).
- The `error-item.js` component is XSS-safe.

---

## Summary Table

| # | Finding | Severity | Spec'd? |
|---|---|---|---|
| 1 | `on_error_mode` column not created | MAJOR | Yes |
| 2 | "Handled not silent" `main[1]` check not implemented | MAJOR | Yes |
| 3 | `AGD_HEALTH_ENABLED` master switch not defined | MAJOR | Yes |
| 4 | Poller/egress env vars not defined | MINOR | Yes (deferred) |
| 5 | Fallback poller not implemented | MINOR | Yes |
| 6 | `get_trace` missing fields | MINOR | N/A |
| 7 | Run-span alignment is a heuristic | MINOR | N/A |
| 8 | `error_summary` not sanitized | MINOR | Yes |
| 9 | Double-broadcast for silent failures | INFO | N/A |
| 10 | `errors` table has no `silent` column | INFO | N/A |
| 11 | Test coverage gaps | MINOR | N/A |
| 12 | Cold start vs retention pruning | MINOR | N/A |

**Totals:** 3 MAJOR, 7 MINOR, 2 INFO

---

## Recommended Priority

1. **Finding 2** (main[1] check) - highest impact on false-positive rate
2. **Finding 3** (`AGD_HEALTH_ENABLED`) - operational safety, quick fix
3. **Finding 1** (`on_error_mode` column) - audit trail completeness
4. **Finding 8** (sanitization) - data exposure risk
5. **Finding 5** (fallback poller) - reliability of enrichment
6. **Finding 11** (test gaps) - idempotency and concurrency tests are high-value
7. Remaining findings - lower priority, can be addressed iteratively
