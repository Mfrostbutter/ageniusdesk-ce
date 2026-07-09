# Silent-Failure Detection (green-but-broken alerts)

**Status:** DRAFT / spec
**Date:** 2026-07-07
**Module:** `backend/modules/observability` (extends existing OTel receiver)
**Depends on:** cost-observability enrichment pattern (`cost.py`), OTLP receiver (`ingest.py`, `router.py`), `n8n_proxy.client.get_execution_raw`

---

## Problem

n8n's execution log reports one thing: the top-level `finished` status. Continue-On-Fail (and half-wired error outputs) decouple node truth from execution truth, so a run that dropped its work still shows green. The affected user never sees it: no failed execution logged, nothing in the dashboard, the integration "just stops working" with no trail. This is the single worst silent-failure class in n8n, and it is exactly what our observability module is positioned to catch, because we already reach into the execution data the log hides.

This is documented from the user side in the bundled skill seed `skills_seed/n8n-error-handling/NODE_ERROR_OUTPUTS.md`. This spec is the detector that makes it loud.

## Empirical grounding (captured 2026-07-07)

A three-mode trap on n8n-dev (webhook → three parallel nodes), execution `11118`, dumped via `get_execution_raw`. Top-level execution: `status: "success"`, `finished: true`, all nodes `status: "success"`. What each failed node actually stored:

| Mode | node.status | itemsOutput | `json.error` | Type |
|---|---|---|---|---|
| HTTP 500, Continue-On-Fail | `success` | 1 | `{name:"AxiosError", code:"ERR_BAD_RESPONSE", status:503, message, stack}` | **object** |
| Code `throw`, Continue-On-Fail | `success` | 1 | `"silent boom [line 1]"` | **string** |
| Zero items (filter matched nothing) | `success` | 0 | *(absent)* | **absence** |

Three findings drive the design:

1. **The `status` field lies at every layer** — top execution, node summary, node status. The detector cannot key on any `status` field. Truth is only in the shape of the output data.
2. **The error shape is inconsistent by source.** HTTP → object with a `.status`; thrown Code → bare string. Same `json.error` key, different type. The detector must type-normalize.
3. **Zero-items carries no error.** It is a different detector entirely (expected-count baseline), not error detection.

Full capture: `scratchpad/silent-failure-rundata-capture.md` (Agenius repo).

## Phase 0 result (RESOLVED 2026-07-07, dogfood on :3066)

Ran the trap on an instrumented n8n (native OTel), read the emitted spans from the dashboard store. Confirmed:

| Node | span status | `n8n.node.items.input` | `n8n.node.items.output` |
|---|---|---|---|
| Webhook | OK | 0 | 1 |
| Mode1 HTTP 500 CoF | **OK** | 1 | 1 |
| Mode2 Code throw CoF | **OK** | 1 | 1 |
| Mode3 Zero items | **OK** | 1 | **0** |

Root `workflow.execute` span: status OK, `n8n.execution.status = "success"`.

Two load-bearing findings:

1. **The exporter marks a Continue-On-Fail node's span `OK`, not ERROR.** Span status propagates the same lie the execution log tells. `list_traces.has_error` (keys on span `status='ERROR'`) will NOT catch modes 1/2. The demoted `json.error` exists only in runData → **runData fetch is mandatory for modes 1/2.**
2. **Node spans carry `n8n.node.items.output`.** Mode 3 shows `output = 0` in the span itself → **zero-items (mode 3) is detectable from spans alone, no runData fetch.** Also confirmed: node spans carry `n8n.node.type`, `n8n.node.id`, `items.input` — enough for per-node baselines without touching the n8n API.

Correlation note: node spans do NOT carry `execution_id`/`workflow_id` (only the root does); they correlate by `trace_id` + node name. `cost.py` already handles this (pulls `exec_id` from whichever span has it) — reuse that.

---

## Design

**Detection splits by cost, per the Phase 0 finding.** Mode 3 is free off the spans we already ingest; modes 1/2 need a runData fetch. New enrichment module `health.py`, a sibling of `cost.py`, same contract: idempotent, best-effort, active-instance-only, bounded fetch.

- **Mode 3 (empty output) — span-only, no fetch.** On ingest, record `n8n.node.items.output` from the span (free). A zero is **recorded, not alerted**, until a per-node baseline says the zero is anomalous — see the baseline rule below. Confirmed necessary in dogfood: a live Email Assistant poll legitimately returns 0 messages on a green run every idle cycle; alerting on that would be pure noise. The MVP already only alerts on `ERROR`, so EMPTY is informational until Phase 2.
- **Modes 1/2 (silent error) — runData fetch required.** The demoted `json.error` has no span signal (status OK, output count normal), so these cannot be pre-filtered: fetch runData once per finished execution, walk each node's output items for a `json.error` key, map to the node span by name. Bounded/timeout like `cost.py`, active instance only. This is the price of catching demoted errors; it is one fetch per execution, not per node.

### Detection rule (the core)

For an execution's `runData` (`raw.data.resultData.runData`, `node_name -> list[runs]`):

- **Silent error** — a run has an error present AND the execution is green. "Error present" = run-level `run.error` is set, OR any item in `run.data.main[0]` has a `json.error` key. Type-normalize:
  - `error` is object → `error_type = error.name or "error"`, `error_summary = error.message or str(error.status)`, capture `http_status = error.status`.
  - `error` is string → `error_type = "thrown"`, `error_summary = error`.
  This flag covers both `continueRegularOutput` (error demoted into `main[0]`) and the "wired to nowhere" `continueErrorOutput` case from `NODE_ERROR_OUTPUTS.md` — both leave an error under a green run.
- **Empty output** — `len(run.data.main[0]) == 0`. Recorded always; only *flagged* against a baseline (Phase 2), because zero is legitimate for many nodes.
- **Handled, not silent** — if the node's error flows out `main[1]` to a wired downstream handler, it is handled. Do not flag. (Detect via presence of a second main output with a downstream connection.)

`health_status` per span ∈ {`OK`, `ERROR`, `EMPTY`, `UNKNOWN`}. Trace-level `is_silent_failure` = trace root `workflow.execute` status is OK/finished-success AND any span `health_status = ERROR`. That is the green-but-broken signal.

### Trigger: eager, not lazy

Cost enrichment is lazy (runs when a user opens a trace) — wrong for an alarm, since nobody opens the trace of a run that looks green. Health enrichment runs **eager**:

- **Primary:** on OTLP ingest (`ingest.ingest_trace_request` already broadcasts `otel:trace`), enqueue health enrichment for each new `execution_id` in the batch.
- **Fallback:** a lightweight execution poller (uses `backend/scheduler.py`) polls the active instance's executions since a stored cursor and runs the health check on each finished execution. This covers the case where OTLP export is not wired at all, and executions that produced no spans. Off by default; on when `AGD_HEALTH_POLL_ENABLED`.

### Storage

Mirror the cost-column ALTER pattern in `database.py` (idempotent `ADD COLUMN`, lands on existing installs). Add to `otel_spans`:

| Column | Type | Meaning |
|---|---|---|
| `health_status` | TEXT | OK / ERROR / EMPTY / UNKNOWN |
| `error_type` | TEXT | AxiosError / thrown / node name of thrower |
| `error_summary` | TEXT | normalized human message (sanitized, truncated) |
| `http_status` | INTEGER | when the error object carried one |
| `output_items` | INTEGER | item count on main[0] |
| `on_error_mode` | TEXT | continueRegularOutput / continueErrorOutput / stopWorkflow (from workflow JSON) |
| `checked_at` | TEXT | enrichment timestamp (idempotency guard, like `priced_at`) |

`storage.py` gains `set_health(updates)` (update by `span_id`, mirrors `set_costs`) and `has_health(trace_id)` (idempotency guard, mirrors `has_cost`). `list_traces` and `metrics_summary` gain `is_silent` / `silent_rate` derived like `has_error` / `error_rate`.

### Reporting (make it loud)

- **Websocket alert.** On detecting a new silent failure, broadcast `otel:silent` `{execution_id, workflow_name, node, error_type, error_summary}`. Frontend raises a toast and badges the Errors view — the run the log called success shows up red.
- **Traces list filter.** A "green-but-broken" filter and a distinct row treatment (green top status + red node marker) in the Observability view.
- **Metrics strip.** Add `silent_failures` and `silent_rate` alongside `error_rate`.
- **Optional egress.** Reuse the existing notification path (Slack/webhook, whatever the fleet uses) for a push alert. Config-gated, off by default.
- **Optional handoff.** A confirmed silent failure is a candidate for the self-healing path (repair the workflow behind the existing gate). Out of scope for MVP; note the seam.

---

## Phases

- **Phase 0 — confirm the exporter's span shape.** Point n8n-dev OTLP export at a running AGD CE (`AGD_OTEL_ENABLED=true`), re-fire the retained trap (`EhBF2KjmLOG3l2Pc`, inactive), read the stored span `status` + `attributes`. Settles whether detection needs runData at all or the span already carries ERROR. ~1 hour.
- **Phase 1 — silent-error detection (MVP).** `health.py` enrichment + schema columns + eager OTLP-ingest trigger + `is_silent` in list/metrics + `otel:silent` websocket + Errors-view surfacing. Covers modes 1/2 (the PSA's core). Tests mirror `test_otel_receiver.py`: synthetic runData through the detector, assert `is_silent`.
- **Phase 2 — low-output anomaly (empty AND magnitude drop).** Per-node classification on the output-count history we already store. Only a node that is historically a reliable producer fires. Full design below ("Phase 2 design"). Covers mode 3 plus band-drops (200 rows → 3).
- **Phase 3 — dead-man's switch (never-ran).** Per-workflow expected cadence (from the schedule trigger, or user-set), scheduled check flags workflows with no execution in-window. Covers the mode with no trace to inspect. Needs the poller from Phase 1's fallback.
- **Phase 4 — egress + self-healing seam.** Push alerts and the repair handoff.

## Phase 2 design: low-output anomaly classification

Not "zero is bad." A per-node **anomaly classifier** on the `output_items` history we already store per `node.execute` span, keyed by `(workflow_id, node_id)` (node_id is stable across renames). Decisions locked 2026-07-09.

### Classify each node from two cheap statistics

Over a rolling window (last N runs or ~30 days) compute **zero-rate** (fraction of runs with output 0) and the **non-zero magnitude** (median/min of the runs that weren't zero). That yields four buckets; only one fires:

| Bucket | Signature | Fires on low output? |
|---|---|---|
| Steady producer | zero-rate ~0, non-zero median ≥ 1 | **Yes** |
| Intermittent / poller | zero-rate moderate (0 and non-0 both common) | No — zero is normal (the Email Assistant case) |
| Dormant | zero-rate ~100% | No — zero is the norm |
| Cold start | fewer than min-samples | No — still learning |

### Precision over recall (locked: yes)

When unsure, stay quiet. Cold-start and intermittent default to **no alert**. Only a steady producer dropping out of its normal band pages. We deliberately under-alert at the margins to keep the alarm trustworthy; a noisy alarm gets muted and becomes worthless. Missed edge cases are recoverable, a distrusted alarm is not.

### Trigger on input-vs-output, and report the origin, not the cascade

The real failure is a node with **`items.input > 0` and `items.output == 0`** (it had work and dropped it). A node with input 0 and output 0 is just inheriting emptiness from upstream. Walk the trace and fire on the **first** node in a chain that went `N → 0`; suppress its downstream victims. This turns "15 nodes all show empty" into one alert at the origin. Core to Phase 2, not optional.

### Magnitude drops in scope (locked: yes)

Same machinery, wider than zero. A steady producer whose output falls far below its historical band (e.g. below `median * drop_factor`, or below the historical min) is a "partial empty" anomaly — catches "200 rows → 3 rows," which a zero-only rule misses. Emitted at a lower confidence than a hard zero.

### Thresholds configurable per instance (locked: yes, from day one)

Ship sane defaults, all overridable via config (env → settings), never hard-coded:

| Knob | Default | Meaning |
|---|---|---|
| `AGD_HEALTH_MIN_SAMPLES` | 20 | below this a node is cold-start (never fires) |
| `AGD_HEALTH_STEADY_ZERO_RATE` | 0.05 | zero-rate at/under which a node is a steady producer |
| `AGD_HEALTH_DORMANT_ZERO_RATE` | 0.95 | zero-rate at/over which a node is dormant |
| `AGD_HEALTH_DROP_FACTOR` | 0.1 | output under `median * this` is a magnitude-drop anomaly |
| `AGD_HEALTH_WINDOW` | 200 runs / 30d | rolling history size |

### Per-node override (what history can't infer)

- **must-be-non-empty** — treat as steady producer immediately (a new critical node with no history that must have data on day one).
- **ignore-empties** — never fire (a node the operator knows is noisy).

### Storage

First cut: none new — the span store *is* the history; query the last N `output_items` for the node_id at detection time (one indexed read). Caveat: the store is retention-pruned (168h here), so a node that runs hourly has ample samples but a weekly node stays cold-start forever (safe: it never false-fires). If low-frequency nodes need coverage, add a small `node_baseline` table that survives pruning (rolling count/zero-rate/median per node_id). Defer until the retention caveat actually bites.

## Testing

- Unit: feed captured runData fixtures (object-error, string-error, zero-items, handled-via-main[1]) to the detector; assert normalization and `is_silent`. Fixtures come straight from execution 11118.
- Integration: extend `test_otel_receiver.py` — ingest a trace whose execution runData (mocked `get_execution_raw`, same pattern as `test_cost_enrichment_from_rundata`) contains a demoted error under a success root; assert the trace surfaces `is_silent` and the `otel:silent` broadcast fires.

## Config (additions)

```
AGD_HEALTH_ENABLED=true          # master switch for silent-failure detection
AGD_HEALTH_POLL_ENABLED=false    # fallback execution poller (when OTLP not wired)
AGD_HEALTH_POLL_INTERVAL_SEC=60
AGD_HEALTH_ALERT_EGRESS=false    # push to Slack/webhook on silent failure
```

## Auto-wire OTLP export on every provisioned n8n (requirement)

Silent-failure detection is only as good as its coverage, and coverage is only automatic if every n8n AgeniusDesk stands up exports its telemetry without a manual step. The product promise is "point n8n at the desk and observability just works"; a hand-wired per-instance export defeats it. So: **every n8n the deployer provisions self-registers its OTLP export with the dashboard's embedded receiver.**

The dashboard already knows its own advertised URL (`settings.agd_public_url`) and the ingest token (`settings.agd_otel_token`), so registration and instrumentation collapse into one step at stand-up time.

Single insertion point: the `n8n_env` list in `backend/modules/docker_mgr/templates.py` (where every provisioned instance's env is assembled). Guarded so it is a no-op when the receiver is off:

```python
# Auto-wire OTLP export: every AGD-provisioned n8n self-registers its telemetry
# with the dashboard's embedded receiver. No manual per-instance step. n8n 2.29.7+
# emits native OTel (workflow.execute / node.execute spans); it appends /v1/traces.
if settings.agd_otel_enabled and settings.agd_public_url:
    endpoint = _container_reachable(settings.agd_public_url).rstrip("/") + "/api/otel"
    n8n_env.append("N8N_OTEL_ENABLED=true")
    n8n_env.append(f"N8N_OTEL_EXPORTER_OTLP_ENDPOINT={endpoint}")
    if settings.agd_otel_token:
        n8n_env.append(
            f"N8N_OTEL_EXPORTER_OTLP_HEADERS=authorization=Bearer {settings.agd_otel_token}"
        )
```

`_container_reachable()` rewrites a `localhost`/`127.0.0.1` public URL to `host.docker.internal` so the endpoint resolves from inside the provisioned container. Reuse the existing host-alias normalizer in `docker_mgr/router.py` (lines ~139-144) rather than reimplementing it.

Failure-mode caution: if the token header is wrong or the endpoint is unreachable, n8n's exporter fails quietly and no spans arrive — a silent failure in the silent-failure wiring. The stand-up post-deploy hook should verify at least one span lands (fire a trivial execution, poll `/api/otel/status` `span_count`) and surface a loud warning if none do. Existing instances are unaffected until redeployed; document that a redeploy is what adopts auto-export.

## Non-goals

- Not editing user workflows (no injected IF-after-node). Detection stays outside the workflow, exactly the "health validation outside the workflow" pattern the forum thread converged on.
- Not trusting any `status` field, span or node, as the detection signal.
- Not the repair itself (self-healing is a separate module; MVP only marks the seam).
