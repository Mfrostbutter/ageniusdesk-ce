# Runaway Workflow Governor (rate trips, in-flight loop kill, and the prevention layer under both)

**Status:** FOR REVIEW
**Date:** 2026-08-19
**Module:** `backend/modules/governor` (new) + `backend/scheduler.py` + `backend/modules/n8n_proxy/client.py`
**Depends on:** the in-process scheduler (`backend/scheduler.py`), OTLP ingest and span store (`observability/ingest.py`, `observability/storage.py`), per-instance n8n client (`n8n_proxy.client`), errors pipeline (`errors.collector.store_error`), messages/toast sinks (`modules/messages`), instance enumeration (`config.get_instances`)

---

## Problem

A workflow can burn real money without ever failing. Nothing in AgeniusDesk currently watches *volume* or *spend rate*, only correctness: every detector we have (silent failure, dead-man's switch layers 1 and 2, heartbeat) answers "did this run produce the right thing," and a runaway produces perfectly correct output, over and over, until the credits are gone.

The motivating incident: an AI email assistant received a burst of spam, replied to each message, drew more inbound from the replies, and consumed an entire Anthropic credit balance. Every individual execution was green. The failure was visible only in aggregate, and only after the fact.

## The two runaway shapes

They look nothing alike in the data, and they need different detectors and different kill switches. Conflating them is the main design risk.

| | **Fan-out** (many executions) | **Loop** (one execution, unbounded work) |
|---|---|---|
| Example | Spam storm drives one reply per inbound message | An agent calls a tool, reasons, calls it again, forever |
| Signal | Execution count per rolling window | Span count and age on a single in-flight trace |
| Visible when | Between executions, near-instantly | During the execution, if OTLP is wired |
| Correct action | Deactivate the workflow (stop new runs) | Stop the execution (the running one is the problem) |
| Wrong action | Stopping one execution: nine more arrive behind it | Deactivating: the running loop keeps looping |

A full trip does both, in order: stop what is running, then deactivate so nothing replaces it.

## What we already have (verified 2026-08-19)

- **Live in-flight visibility through spans.** `ingest.py:207` treats the arrival of the `workflow.execute` root as the *completion* signal, which means child `node.execute` spans land in earlier batches, mid-execution. A trace accumulating spans with no root yet is a run still in progress. Nothing needs to change to see this.
- **Live in-flight visibility without spans.** n8n's `GET /executions?status=running` is documented on this version, and `client.list_executions` already passes `status` straight through (`client.py:550`). `insights/aggregator.py:159` already counts running executions.
- **A kill switch for each shape.** `client.set_workflow_active(workflow_id, False)` (`client.py:1211`) handles the fan-out case. For the loop case the n8n **public** API exposes, verified against n8n-dev's OpenAPI document:
  - `POST /executions/{id}/stop`, required scope `execution:stop`
  - `POST /executions/stop`, body `{status: ["queued"|"running"|"waiting"], workflowId?}`, same scope

  Neither is wrapped in `n8n_proxy.client` yet. Both are small additions in the established `_post` style.
- **A place to run the sweep.** `scheduler.register(job_id, func, interval_fn, enabled_fn)`, 30-second tick, live-read config callables. Only the backups job is registered today (`main.py:115`).
- **Aggregates.** `storage.metrics_summary(instance_id, window_hours, workflow_id)` already returns executions, `throughput_per_hr`, and `spend_usd` over a window, scoped to one workflow when asked.
- **Prevention primitives already writable.** `_ALLOWED_WF_SETTINGS` (`client.py:878`) includes `executionTimeout`, and `put_workflow_full` round-trips `settings`. The Agent node exposes `options.maxIterations` (default 10).
- **Surfacing.** `errors.collector.store_error()` persists and broadcasts; the messages module carries toasts with optional Slack and Discord sinks.

## Design

### 1. Layer 0: prevention, and it ships first

n8n enforces `settings.executionTimeout` itself, in-process, with no dependency on AgeniusDesk being awake, reachable, or correct. That is a strictly better failure mode than any poller we can write, and it would have ended the motivating incident on its own.

Layer 0 is an audit plus a bulk remediation, with no detection logic at all:

- Sweep every workflow on every instance. Report those with no `executionTimeout`, and those whose Agent nodes have `options.maxIterations` absent or raised above a configured ceiling.
- Offer bulk remediation: set a default timeout on the selected workflows via `put_workflow_full`.
- Surface as a standing posture panel ("14 of 62 workflows have no execution timeout"), not as an alert.

Note that `put_workflow_full` deactivates and reactivates around the write. Remediating an active workflow therefore blips it. Batch remediation must be an explicit, operator-initiated action, never a scheduled auto-fix.

### 2. Layer 1: the rate governor (fan-out)

A scheduler job, default interval 60s. Per (instance, workflow) over a rolling window:

- **executions per window**, from `metrics_summary` where OTLP is wired, falling back to `list_executions` counts where it is not
- **estimated spend per window**, from the same call

Static thresholds first, not learned baselines. Learned p95 bands are the obvious next step and the wrong first step: static numbers the operator sets are easier to trust, easier to explain when they fire, and easier to debug when they do not. Cold start stays silent, consistent with the health detectors: below `agd_gov_min_samples` observations, never trip.

Both a per-workflow and an instance-wide ceiling. A fan-out that spreads across five workflows slips under every per-workflow limit while still draining the account.

### 3. Layer 2: the in-flight detector (loop)

Two candidate sources, both cheap:

- **Span-derived:** a trace with no `workflow.execute` root whose span count exceeds `agd_gov_max_spans_per_run`, or whose age since first span exceeds `agd_gov_max_run_age_sec`.
- **API-derived:** an execution reported `running` whose `startedAt` is older than the age threshold. Works without OTLP.

**The load-bearing rule, mirroring the heartbeat spec's anti-false-positive gate: a rootless trace is ambiguous.** It means either "still running" or "we lost the root span" (export gap, dropped batch, dashboard restart). Those are indistinguishable from inside our own store, and one of them is a perfectly healthy finished run. So the span signal *nominates*, it never acts. Before any kill, confirm against the owning instance with `GET /executions/{id}` and proceed only if n8n itself reports the execution `running`.

Three exclusions, all mandatory:

- **`waiting` is never a runaway.** A Wait node makes an execution legitimately long-lived. Only `running` is eligible.
- **`manual` mode is excluded by default.** A developer running a long test in the editor should not have it killed out from under them.
- **Sub-workflow children.** A parent that calls sub-workflows produces several concurrent executions. Killing the parent leaves children orphaned, and vice versa. v1 acts on the execution it nominated and logs the relationship rather than trying to walk the tree.

### 4. The kill, and its blast radius

Ordered narrow to wide. The governor picks the narrowest action that addresses the shape it detected:

1. `POST /executions/{id}/stop` on the specific nominated execution (loop case)
2. `POST /executions/stop` with `{status:["running"], workflowId}` when the operator has opted into "stop everything running for this workflow"
3. `set_workflow_active(id, False)` to stop new runs (fan-out case)

Stop sets a cancellation flag. A node blocked inside a single long HTTP call will not abort until that call returns; for an agent looping over many short tool calls, cancellation lands between iterations, which is the case this targets. Behavior under queue mode (multiple workers) is untested and flagged below.

Deactivation drops webhook registrations, so inbound arriving during a trip is lost rather than queued. That is the correct tradeoff for a real runaway and the wrong one for a false positive, which is the whole reason for dry-run defaults.

### 5. Trip state machine and re-arm

A trip is a recorded state, never a silent flap.

`ok -> warned -> tripped -> (manual) re-armed`

- `warned`: threshold crossed, action not taken, notification sent. Returning to `ok` clears it after a cooldown.
- `tripped`: action taken, recorded with the evidence (which threshold, observed value, action, API response).
- **Re-arm is always manual.** The governor never reactivates a workflow it deactivated. Auto-recovery on a runaway means a loop that trips, recovers, and trips again all night.

### 6. Storage

New tables in the existing SQLite database, module-owned:

- `governor_rules` (id, instance_id, workflow_id nullable for instance-wide, metric, window_sec, threshold, action, enabled, dry_run)
- `governor_trips` (id, rule_id, instance_id, workflow_id, execution_id, metric, observed, threshold, action_taken, action_result, state, occurred_at, rearmed_at, rearmed_by)

Trips are audit records and outlive spans deliberately: span retention is 168 hours, and "why did this deactivate itself last month" needs to survive that.

### 7. Surfacing

Trips route through `errors.collector.store_error()` (persisted, broadcast, visible in Overview / Errors / Insights without a new UI surface) plus a toast. A trip that took an action is high severity by definition. Dry-run trips are recorded and shown but marked, so a week of dry-run reads as a tuning log rather than an incident list.

### 8. Config

Following the `agd_health_*` pattern in `backend/config.py`, per-instance overridable:

| Key | Default | Meaning |
|---|---|---|
| `agd_gov_enabled` | `false` | Master switch |
| `agd_gov_dry_run` | `true` | Detect and record, never act |
| `agd_gov_interval_sec` | `60` | Sweep cadence |
| `agd_gov_min_samples` | `20` | Below this, never trip (cold start) |
| `agd_gov_max_runs_per_window` | (unset) | Fan-out ceiling, per workflow |
| `agd_gov_window_sec` | `300` | Rate window |
| `agd_gov_max_spend_per_hour` | (unset) | Estimated spend ceiling |
| `agd_gov_max_spans_per_run` | `500` | Loop nomination threshold |
| `agd_gov_max_run_age_sec` | `900` | Loop nomination threshold |
| `agd_gov_exclude_manual` | `true` | Skip editor runs |

`agd_gov_dry_run` defaulting to true is deliberate and should stay true through at least one full soak on 3066.

## The cost blind spot

Cost enrichment runs at completion: `ingest.py` schedules `cost.enrich_trace(tid)` when the root span arrives. An in-flight execution therefore has **no priced spend at all**. Layer 2 trades on span counts as a proxy for money, which is fine for catching a loop and useless for a rule like "kill any single execution that exceeds five dollars."

Closing that would mean pricing incrementally as `node.execute` spans land, which means fetching run-data mid-execution (expensive, and the run-data for an unfinished execution is partial). Out of scope for v1, documented here so nobody designs a dollar-denominated in-flight rule on top of a signal that cannot support it.

Separately, the dollar figures are computed from the local price book (`cost_is_estimate` exists as a column for exactly this reason). AgeniusDesk cannot see a provider credit balance. **A provider-side spend limit remains the real backstop**, because a governor inside the control plane can only act on data it received, and it sees nothing while its own container is restarting.

## Precision over recall, applied

Consistent with the silent-failure and heartbeat doctrine, and more strictly here because the action is destructive rather than merely noisy:

- Never act on our own telemetry alone. Confirm against the source instance (section 3).
- Never act below `agd_gov_min_samples`.
- Never act on `waiting` or `manual`.
- Default to the narrowest action that fits the detected shape.
- Dry-run by default; opt in per workflow to let it pull a trigger.

## Edge cases

- **An export gap looks exactly like a loop.** Handled by the confirm-before-acting gate. This is the single most likely false positive.
- **Legitimate burst.** A backfill or batch import genuinely runs a workflow 400 times in ten minutes. Needs a per-workflow exemption, and is the strongest argument for keeping v1 opt-in per workflow rather than fleet-wide.
- **Instance unreachable.** Cannot confirm, so cannot act. Log and skip; never act on stale local data.
- **Clock skew** between the n8n host and AgeniusDesk distorts age-based nomination. Prefer `received_at` (our clock) over `startedAt` (theirs) where both are available.
- **API key scope.** See open questions.

## Phases

1. **Layer 0**: audit and bulk remediation for `executionTimeout` and `maxIterations`. Independently useful, ships alone.
2. **Storage and sweep scaffolding**, rate detector, dry-run only.
3. **In-flight detector** with the confirmation gate, dry-run only.
4. **Actions enabled** behind per-workflow opt-in, after a soak.
5. **UI**: rules editor, trip log, re-arm button.

## Testing

Follow the existing patterns: fake n8n client, injected clock, no live instance in the suite.

- Rate detector trips at threshold, stays silent below `min_samples`.
- Rootless-trace nomination followed by a confirmation that reports `success`: **no action taken**. This is the export-gap case and the most important test in the spec.
- `waiting` and `manual` executions are never nominated.
- Dry-run records a trip and calls no mutating client method (assert on the fake).
- A tripped workflow is not auto-re-armed by a subsequent healthy sweep.
- Stop and deactivate client wrappers surface n8n's error body on failure, matching `set_workflow_active`.

## Non-goals

- Learned or seasonal baselines (static thresholds only in v1).
- Intra-execution incremental pricing.
- Killing at sub-workflow tree granularity.
- Anything that reactivates a workflow automatically.
- Reading provider-side balances or quotas.

## Open questions for review

1. **API key scope.** These endpoints require `execution:stop`. The keys AgeniusDesk currently holds were minted for read plus workflow management and probably lack it. Re-issuing keys is a manual step on every instance and gates all of layer 2. Confirm before building past phase 3.
2. **Default action for the fan-out case:** deactivate, or alert only? Deactivation is the useful one and also the one that loses inbound webhooks.
3. **Instance-wide spend ceiling in v1, or per-workflow only?** The motivating incident was a single workflow, but the cross-workflow gap is real.
4. **Which loop shape was the actual incident?** Whether the repetition sat inside the agent (`maxIterations` raised) or at the workflow level (a loop-back edge or sub-workflow recursion) changes which layer would have caught it, and is worth confirming from the surviving executions before tuning defaults.
5. **Queue mode.** Stop semantics with multiple workers are untested here.
6. **Trip retention.** Trips outlive spans by design. How long, and do they belong in the backup set?
