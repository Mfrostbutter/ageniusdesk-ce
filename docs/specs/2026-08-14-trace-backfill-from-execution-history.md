# Trace Backfill from n8n Execution History

**Status:** Phase 1 BUILT and validated in production (2026-08-15). Phase 2 not built.
**Date:** 2026-08-14
**Decisions locked:** 2026-08-14 (see "Decisions" below)
**Module:** `backend/modules/observability` (new `backfill.py`) + `backend/scheduler.py` + Observe view
**Depends on:** span storage (`observability.storage.insert_spans` / `trace_id_for_execution`), the run-data fetch path (`n8n_proxy.client.get_execution_raw_for` / `get_execution_raw_by_instance`, added for cross-instance enrichment in v0.5.0), the enrichers (`cost.enrich_trace`, `health.enrich_trace_health`), instance enumeration (`config.get_instances`), the in-process scheduler (`backend/scheduler.py`)

---

## Problem

The span store is the **only** record of an execution's shape, and it is lossy in three ways that all produce the same result: a permanent hole in observability that no amount of waiting fixes.

1. **The receiver was not accepting.** Trace export is fire-and-forget. If AgeniusDesk rejects or is unreachable, n8n logs an exporter error and drops the batch. There is no retry and no queue.
2. **Retention rolled it off.** `AGD_OTEL_RETENTION_HOURS` (default 168) and `AGD_OTEL_MAX_SPANS` prune old spans by design.
3. **Export was never wired.** An instance connected by URL exports nothing until someone configures it, so its entire history is absent.

Case 1 is not hypothetical. On this fleet the dashboard's `AGD_OTEL_TOKEN` and the n8n container's `N8N_OTEL_EXPORTER_OTLP_HEADERS` bearer drifted apart. From 2026-08-13 18:05 until 2026-08-14 22:31 every export was answered `401 Invalid or missing OTel token`, n8n logged `OTLPExporterError: Unauthorized` after each run, and every trace in that window was lost permanently. The dashboard showed an empty Observe view the whole time. Roughly 28 hours of production executions are simply gone, and nothing in the product can recover them.

**But n8n still has the data.** It records every execution's per-node timing in its own database, independent of OTel entirely. The traces are not actually gone; they are unreconstructed. This spec turns the span store from a write-once sink into something rebuildable from the source of truth.

## What n8n retains (verified)

Measured on `n8n-dev` (n8n 2.33.7), 2026-08-14:

- 365 executions retained, `2026-03-18` to `2026-08-14`, with no `EXECUTIONS_DATA_*` pruning vars set.
- `execution_entity`: `id`, `workflowId`, `status`, `startedAt`, `stoppedAt`, `mode`.
- `execution_data.data`: the run payload in `flatted` form (values hoisted into an array, referenced by index).

Per node run, `resultData.runData[nodeName][runIndex]` carries:

| Field | Use |
| --- | --- |
| `startTime` | span start (epoch ms) |
| `executionTime` | span duration (ms) |
| `executionStatus` | span status |
| `data.main[branch]` | output item counts per branch |
| `source` | upstream node, i.e. the parent edge and input counts |
| `error` | per-node error for a failed or continued node |

Sampled from execution 25173 (`[silent-test] order-sync`):

```
Manual Trigger      start=1786746696286 dur=3ms   status=success out_items=[1]
Get Orders          start=1786746696301 dur=282ms status=success out_items=[1]
Parse & Validate    start=1786746696597 dur=60ms  status=success out_items=[0]
```

The real captured trace for that same execution holds **4 spans**: one `workflow.execute` root plus three `node.execute` children. `runData` holds exactly those three nodes. The mapping is 1:1.

**The API already un-flattens this.** `GET /api/v1/executions/{id}?includeData=true` returns `data.resultData.runData` as ordinary JSON, and AgeniusDesk already calls it (`client.py:556`, `:607`, `:631`) to drive cost and health enrichment. No `flatted` parser and no direct Postgres access is needed.

## Target span shape

Synthesized spans must be indistinguishable to every existing consumer (waterfall, metrics strip, health detectors, cost enricher). Observed shape of a real n8n-exported trace:

**Root**: `name="workflow.execute"`, `kind=1`, no parent:
`n8n.execution.id`, `n8n.execution.mode`, `n8n.execution.status`, `n8n.execution.is_retry`, `n8n.workflow.id`, `n8n.workflow.name`, `n8n.workflow.node_count`, `n8n.workflow.version_id`, `n8n.project.id`

**Child, one per node run**: `name="node.execute"`, `kind=1`, parent = root `span_id`:
`n8n.node.id`, `n8n.node.name`, `n8n.node.type`, `n8n.node.type_version`, `n8n.node.items.input`, `n8n.node.items.output`

Every one of these is derivable: node identity and type from `workflowData.nodes` matched by name, item counts from `runData` `data.main` lengths and `source`, execution fields from the execution record.

## Fidelity

| Rebuilds exactly | Rebuilds approximately | Cannot rebuild |
| --- | --- | --- |
| Root + per-node spans, real timings and ordering | Root span bounds (from `startedAt`/`stoppedAt`, which include queue/startup time the exporter measured slightly differently) | Sub-node spans native OTel emits that `runData` never held: individual HTTP calls inside a node, retry attempts |
| Parent/child edges from `source` | Wall-clock skew between n8n's clock and the exporter's | True exporter `trace_id` / `span_id` |
| Per-node status and errors | | Spans for executions n8n pruned or never saved (`saveDataSuccessExecution=false`) |
| Output/input item counts, so silent-failure and dead-man's-switch detection both run | | |
| LLM token usage and cost, since `cost.py` already reads this same `runData` | | |

Fidelity is high enough that a rebuilt trace is useful for every purpose the real one served except sub-node drill-down.

## Design

### 1. A synthesizer, not a second ingest path

New `observability/backfill.py` exposes:

```python
async def synthesize(execution_raw: dict, instance_id: str) -> list[dict]
async def backfill_execution(execution_id: str, instance_id: str) -> int
async def backfill_instance(instance_id: str, since: str, until: str, limit: int) -> dict
```

`synthesize` is pure: raw execution payload in, `otel_spans` row dicts out, matching `ingest.parse_request`'s output contract exactly. Everything downstream (`storage.insert_spans`, prune, enrichers, the WebSocket broadcast) is reused unchanged. Keeping it pure also makes it directly testable against a recorded fixture with no network and no database.

### 2. Deterministic, collision-free, self-identifying ids

Real ids come from the exporter and are unavailable. Synthesize them:

```
trace_id = sha256(f"agd-backfill:{instance_id}:{execution_id}").hexdigest()[:32]
span_id  = sha256(f"{trace_id}:{node_name}:{run_index}").hexdigest()[:16]
```

Deterministic derivation gives idempotency for free: re-running a backfill over the same execution produces byte-identical ids, so a re-run is a no-op rather than a duplicate. The `agd-backfill:` domain prefix keeps the space disjoint from real 128-bit exporter ids.

### 3. Synthetic spans are labeled

Add a nullable `origin` column to `otel_spans` (`NULL`/`'otlp'` = received, `'backfill'` = synthesized), set on insert. Required because:

- The waterfall should tell an operator a trace was reconstructed, so missing sub-node detail reads as "not captured" rather than "did not happen."
- A real trace arriving later for the same execution must win. See §4.
- Operators need to distinguish a genuine ingest outage from a healthy backfilled window when auditing coverage.

Migration follows the existing additive pattern in `database.py`: `ALTER TABLE ... ADD COLUMN` guarded by a column check, no rewrite, older rows read as `NULL` and mean "received."

### 4. Never overwrite a real trace

Before synthesizing, check `storage.trace_id_for_execution(execution_id)`. If a trace exists **and** its origin is not `backfill`, skip. Real telemetry always outranks a reconstruction.

If a real trace arrives *after* a backfill (out-of-order export, a delayed batch, or an operator re-running an old execution), ingest should delete the backfilled spans for that `(instance_id, execution_id)` before inserting. This is the one change inside `ingest.py`: a targeted delete keyed on `origin='backfill'`, which is a no-op in the overwhelmingly common case where nothing was backfilled.

### 5. Bounded by the retention window in v1

`storage.prune` deletes by `received_at`, and `metrics_summary` windows by `received_at`. That creates a genuine conflict for historical spans, and picking either value naively breaks something:

- `received_at = now` → a three-week-old execution counts toward "Executions (24h)" and the Spend metric. The metrics strip becomes a lie.
- `received_at = execution start` → `prune` deletes it on the very next ingest if it falls outside `AGD_OTEL_RETENTION_HOURS`. The backfill silently undoes itself.

**v1 decision: set `received_at` to the execution's true start time, and refuse to backfill beyond the configured retention window.** You cannot rebuild further back than AgeniusDesk would have kept anyway. This keeps prune and every window query honest with no semantic changes, and the refusal is explainable in one sentence in the UI. It directly solves the motivating case, since a 28-hour hole sits comfortably inside a 168-hour default.

**Deferred to a follow-up:** decouple event time from ingest time properly (window and prune on `start_ns`, keep `received_at` as an audit field) so an operator can rebuild an arbitrarily deep archive. That is a broader change to retention semantics and deserves its own review rather than riding along here.

### 6. Phase 1: on-demand backfill

Endpoints on the existing observability router:

- `GET /api/otel/backfill/preview?instance_id=&since=&until=`: how many executions in range have no trace, how many are outside the retention window, how many lack saved data. Costs one `list_executions` page walk, no run-data fetches.
- `POST /api/otel/backfill/run`: execute it. Operator role, CSRF-checked, consistent with other state-changing routes.

Bounding, since this fans out to n8n:
- Hard cap per run (`AGD_BACKFILL_MAX_EXECUTIONS`, default 500).
- Bounded concurrency (default 4) against a single instance, reusing the fan-out shape `list_instances_health` already established.
- Fetch run-data through `get_execution_raw_for(inst, ...)` so credentials and TLS resolve against the **owning** instance, matching the v0.5.0 attribution fix.
- Progress broadcast over the existing WebSocket so the UI can show a live count.

UI: a **Rebuild traces** action on Observe. It is the natural place to put it, because the empty state added on 2026-08-14 (QA-001) is exactly where an operator lands after an outage. When an instance has zero spans but n8n reports executions, that panel should offer the rebuild directly.

### 7. Phase 2: scheduled gap-fill

Register a `trace_backfill` job with the existing scheduler (`interval_fn` / `enabled_fn` read config live, so toggling takes effect without a restart). Each tick, per instance: list completed executions in the last N minutes, drop those that already have a trace, synthesize the rest.

This changes the character of the whole pipeline: **OTLP ingest becomes best-effort**, because anything it drops is reconciled within one tick. A token mismatch, a dashboard restart during a deploy, a network blip mid-export, all self-heal.

Off by default. It is a recurring outbound API load against every configured instance, and that should be an operator's explicit choice.

## Configuration

| Var | Default | Meaning |
| --- | --- | --- |
| `AGD_BACKFILL_ENABLED` | `false` | Phase 2 scheduled gap-fill |
| `AGD_BACKFILL_INTERVAL_SEC` | `900` | Sweep cadence |
| `AGD_BACKFILL_LOOKBACK_MIN` | `120` | How far back each sweep looks |
| `AGD_BACKFILL_MAX_EXECUTIONS` | `500` | Per-run cap (both phases) |
| `AGD_BACKFILL_CONCURRENCY` | `4` | Parallel run-data fetches per instance |

Phase 1 needs none of these to be useful; it is fully manual.

## Non-goals

- Rebuilding sub-node spans. The data does not exist in `runData`.
- Backfilling from anything but n8n's API. No direct Postgres reads, no `flatted` parsing.
- Rebuilding beyond the retention window in v1 (see §5).
- Replacing the OTLP receiver. Backfill is a recovery and reconciliation path; live export stays the primary source and always outranks a reconstruction.

## Risks

| Risk | Mitigation |
| --- | --- |
| Hammering a production n8n with run-data fetches | Hard caps, bounded concurrency, opt-in scheduling, preview before run |
| A rebuilt trace mistaken for a captured one | `origin` column, surfaced in the waterfall |
| Duplicate spans from repeated runs | Deterministic ids make re-runs idempotent |
| Backfilled spans skewing "last 24h" metrics | Event-time `received_at` (§5) |
| Silent-failure detection firing retroactively on a rebuilt window | Health enrichment writes into the errors pipeline, so a deep backfill could produce a burst of stale alerts. **Open question below.** |

## Decisions

Locked 2026-08-14.

1. **Health enrichment runs on backfilled traces. YES.** A silent failure missed during an ingest outage stays catchable after the fact, which is a large part of the point. Both enrichers therefore run on synthesized traces: `cost.enrich_trace` always, `health.enrich_trace_health` always. The request carries a `detect_health` flag (default `true`) so an operator rebuilding a deep archive purely for waterfall history can turn detection off for that run.

   *Carried risk, accepted:* health enrichment writes into the errors pipeline, so rebuilding a large window can post a batch of incidents that are already over. Mitigation at build time is presentation, not suppression: a silent failure raised from a backfilled trace should carry its **execution time**, not ingest time, so the Errors feed sorts it into history rather than surfacing it as breaking news. If that proves insufficient in practice, the fallback is the alerting-window guard (only detect inside 24h), but we are not building that guard preemptively.

2. **Phase 1 ships alone first. YES.** On-demand rebuild over a range solves the recovery case completely. Phase 2 (scheduled gap-fill) is the same synthesizer behind the scheduler, but it puts recurring outbound load on production instances and deserves to land as its own change once Phase 1 is proven. Phase 2 stays specced, unbuilt.

3. **`saveDataSuccessExecution=false` raises a coverage warning. YES.** An instance configured that way can never be backfilled for successful runs, and discovering that at recovery time is the worst possible moment. Detect it via the instance's settings API and surface it as a coverage warning on the Instances view, with the same warning repeated in the backfill preview when the selected instance is affected.

## Phase 1 build plan

Ordered, each step independently verifiable.

1. **Schema:** add the nullable `origin` column to `otel_spans` via the existing guarded `ALTER TABLE` pattern in `database.py`. Verify older rows read `NULL`.
2. **Synthesizer:** `observability/backfill.py` with a pure `synthesize(execution_raw, instance_id) -> list[dict]`, output matching `ingest.parse_request`'s row contract. Record the raw payload for execution 25173 as `tests/fixtures/`, and assert the rebuild against that execution's real captured 4-span trace (span count, names, parent edges, node ids, item counts, durations).
3. **Single-execution path:** `backfill_execution(execution_id, instance_id)`: precedence check via `trace_id_for_execution`, fetch through `get_execution_raw_for` (owning instance credentials), insert, then run both enrichers. Assert idempotency by running it twice.
4. **Range path:** `backfill_instance(instance_id, since, until, limit)`: page `list_executions`, skip those already traced, apply the retention-window refusal, bounded concurrency, per-run cap.
5. **Ingest precedence:** in `ingest.py`, delete `origin='backfill'` spans for an `(instance_id, execution_id)` before inserting real ones, so a late real trace replaces a reconstruction.
6. **API:** `GET /api/otel/backfill/preview` and `POST /api/otel/backfill/run` (operator role, CSRF), with WebSocket progress.
7. **UI:** a **Rebuild traces** action on Observe, offered directly from the zero-span empty state added in QA-001, since that is where an operator lands after an outage.
8. **Coverage warning:** detect `saveDataSuccessExecution=false` per instance; surface on Instances and in the backfill preview.

**First real-world validation:** rebuild the 2026-08-13 18:05 → 2026-08-14 22:31 window on the `Test` instance, the ~28 hours lost to the token mismatch. That gap is inside the 168h retention window, n8n retained the executions, and it is the exact scenario the feature exists for.

**Validation result (2026-08-15):** ran on the dogfood instance against the lost window. Summary: 71 scanned, 69 backfilled (294 spans across 5 workflows), 2 skipped for existing real traces, 0 outside retention, 0 errors. Health enrichment ran on 225 spans and cost enrichment priced 15 LLM spans; the window produced no silent failures, which matches the live record on either side of the gap. A second run over the same window re-synthesized all 69 and inserted 0 rows, confirming idempotency on production data. Live OTLP ingest unaffected.

## Testing

- **Golden fixture:** the recorded raw payload for execution 25173, whose real 4-span trace is captured. Assert the synthesizer reproduces span count, names, parent edges, node ids, item counts, and per-node durations. This is the strongest available test, comparing a rebuild against a genuine exporter trace of the same run.
- Idempotency: backfilling twice inserts once.
- Precedence: a real trace arriving after a backfill replaces it; a backfill never replaces a real trace.
- Retention refusal: an execution older than `AGD_OTEL_RETENTION_HOURS` is reported, not silently skipped.
- Detector compatibility: a synthesized trace with a zero-output steady producer trips silent-failure detection identically to a received one.
- Bounding: caps and concurrency limits hold against a stubbed slow instance.

## Related

- QA-006 (unlogged): a rejected OTLP export is invisible on the dashboard. AgeniusDesk returns 401 and moves on while n8n logs the failure into its own container logs, which is why the 28-hour gap went unnoticed. A rejected-ingest counter on Observe would have made it a ten-second diagnosis. Backfill is the cure; that counter is the alarm, and the two are complementary.
- [Silent-failure detection](2026-07-07-silent-failure-detection.md) and [dead-man's switch layer 2](2026-07-11-heartbeat-dead-mans-switch-layer-2.md), both of which consume the span store this feature repairs.
- [Instance attribution](../architecture/instance-attribution.md), whose per-instance credential path this reuses.
