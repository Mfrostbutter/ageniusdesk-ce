# Heartbeat — Dead-Man's Switch Layer 2 (workflow never fired)

**Status:** FOR REVIEW
**Date:** 2026-07-11
**Module:** `backend/modules/observability` (new `heartbeat.py`) + `backend/scheduler.py`
**Depends on:** dead-man's-switch Layer 1 (`health._missing_candidates`, commit `4a2d4d7`), the in-process scheduler (`backend/scheduler.py`), per-instance n8n client (`n8n_proxy.client.list_workflows` / `get_workflow` / `list_executions`), errors pipeline (`errors.collector.store_error`), instance enumeration (`config.get_instances`)

---

## Problem

Every detector we have is **reactive to a trace**. A span or run-data record arrives, we enrich it, and if it looks wrong under a green run we flag it. Layer 1 of the dead-man's switch already catches a node that *should have run and didn't* **within** an execution that did fire (`health._missing_candidates`): it diffs the workflow's declared nodes against the spans that landed.

Layer 2 is the case with **no execution at all**. A scheduled workflow that should run hourly silently stops firing. A webhook-triggered integration whose upstream caller broke stops calling. There is no failed run, no green run, no span, no run-data, nothing to enrich. The absence of any signal *is* the failure, and nothing that keys on an incoming trace can ever see it. This is the last gap the spec's original "Phase 3 — dead-man's switch (never-ran)" named, and it needs its own mechanism: a **periodic sweep**, not an enrichment.

## Why this is structurally different

| | Layers already shipped | Layer 2 heartbeat |
|---|---|---|
| Trigger | An OTLP ingest / run-data fetch | A wall-clock timer |
| Input | A trace that exists | The *set of workflows that should have produced a trace by now* |
| Signal | A span/run looks wrong | No run arrived inside the expected window |
| Attaches to | A `span_id` | Nothing — there is no span (surfaces via the errors pipeline, like Layer 1's `did_not_run`) |
| Failure mode to avoid | False positive on a legit empty | **False positive when the telemetry is missing but the workflow is fine** |

That last row is the whole design risk. Missing telemetry (OTLP not wired, exporter dropped, dashboard restarted) looks *identical* to a workflow that stopped firing, if we judge from our own span store. So the load-bearing rule is: **never raise a heartbeat alarm from the absence of our own telemetry. Confirm against the source instance's own execution list before alarming.** This mirrors the instance-attribution learn-and-pin philosophy (probe the owning instance to establish ground truth, don't infer from what we happened to receive).

## What data we already have

- `config.get_instances()` enumerates configured instances; `list_instances_health` already iterates each instance and hits *its own* API (`workflows_active` count), so per-instance fan-out is an established pattern.
- `n8n_proxy.client.list_workflows(active=true)` — the active workflow set per instance (the population to watch).
- `get_workflow(id)` returns the workflow definition (nodes/params) — the schedule trigger and its interval live here.
- `list_executions(...)` — the authoritative "when did this workflow last actually run" from n8n itself, independent of whether OTLP export is wired.
- `_detect_trigger_type(workflow)` already classifies `schedule` / `webhook` / `manual` / `error` / `unknown`. Extend it to also return the **schedule interval**, don't reimplement trigger detection.
- `scheduler.register(job_id, func, interval_fn, enabled_fn)` — the sweep host. Config (enabled, interval) is read live every tick through callables, so a hot config reload takes effect without a restart.
- `errors.collector.store_error(...)` — the surfacing seam Layer 1 uses; broadcasts an `error` event so the alarm shows live across Overview / Errors / Insights.

## Design

### 1. The watched set comes from n8n, not from our span store

The population of workflows to monitor is `list_workflows(active=true)` per instance, refreshed each sweep. Rationale:

- A workflow that has **never** exported a span is invisible to our store but still needs watching — the n8n API is the only complete source.
- A workflow **deactivated** in n8n must silently drop out of the watched set (deactivation is intentional, not a failure). Sourcing from `active=true` gives us that for free.
- A **manual**-trigger workflow has no cadence to violate; exclude it. An **error**-trigger workflow only runs on other failures; exclude it. Only `schedule` and (opt-in) `webhook` are watchable.

### 2. Expected cadence — three sources, tiered by precision

Precision-over-recall is already the locked doctrine for this feature (spec §"Precision over recall"). Apply it here by tiering the cadence sources and only turning the trustworthy one on by default.

**Tier A — schedule-derived (default ON).** For a `schedule`-trigger workflow, parse the Schedule Trigger node's rule into an interval. n8n's rule shapes:
- Fixed interval: `rule.interval = [{field:"hours", hoursInterval:1}]` / `minutesInterval` / `daysInterval` → seconds directly.
- Cron: `rule.interval = [{field:"cronExpression", expression:"0 * * * *"}]` → compute the expected next-fire time from the last-seen fire. This needs a cron evaluator (`croniter`), which is **not currently a backend dependency — flagged as a review decision below**.

Expected-by = `last_fired_at + interval + grace`. If `now > expected_by`, the workflow is **overdue**. Grace absorbs scheduler jitter and export lag (default = `max(interval * 0.5, AGD_HEARTBEAT_MIN_GRACE_SEC)`).

**Tier B — learned cadence (default OFF, opt-in per workflow or per instance).** For a `webhook`/event workflow there is no declared cadence. Derive an inter-arrival band from execution history (median + p95 of gaps over the rolling window). Overdue = gap-since-last far exceeds the historical p95 (× a slack factor). This is inherently lower-precision (bursty/seasonal webhooks produce long legit gaps), so it stays off unless the operator opts a workflow in. When history is too short to be sure, do not watch (cold-start = silent, consistent with Layer 1 and Phase 2).

**Tier C — user-set expected interval (always available, highest authority).** A per-workflow override: "this must run at least every X." Overrides both A and B. This is the escape hatch for a critical webhook the operator knows the cadence of, and for correcting a mis-parsed schedule. Same override surface as Phase 2's per-node `must-be-non-empty` / `ignore-empties`.

### 3. Confirm before alarming (the anti-false-positive gate)

When the sweep computes a workflow as overdue **from our own data**, it does not alarm yet. It confirms against the owning instance:

1. Query that instance's `list_executions(workflow_id, limit=1)` for the true last-execution timestamp.
2. If n8n reports a real execution inside the expected window, our telemetry simply missed it (export gap) — **do not alarm**, update `last_fired_at` from the API, and (optionally) emit a low-severity `telemetry_gap` note so the export wiring problem is itself visible.
3. Only if n8n *also* confirms no execution inside the window does the workflow alarm as `did_not_fire`.

This is one bounded API call per *overdue-suspected* workflow per sweep, not per workflow — healthy workflows never reach this branch.

### 4. Instance-down disambiguation

If an instance's API is unreachable this sweep, every one of its workflows would look overdue. That is one failure (the instance / its credentials / the network), not N workflow failures. So:

- A sweep that cannot reach an instance raises **one** `instance_unreachable` alarm for that instance and **suppresses all per-workflow heartbeat checks** for it that tick.
- Per-workflow `did_not_fire` alarms only fire when the instance answered and confirmed the silence.

### 5. Fire once per outage — the heartbeat state machine

A sweep runs every few minutes; an overdue workflow must not re-alarm every tick. Per `(instance_id, workflow_id)` keep a state ∈ `{ok, overdue, muted}`:

- `ok → overdue`: emit `did_not_fire` **once**, stamp `last_alerted_at`.
- `overdue → overdue`: no new alarm (optionally re-notify egress after a long re-alert interval; default no).
- `overdue → ok`: the workflow fired again → emit a `recovered` event so the alarm clears in the UI (parallels the Layer-1 silent-clear path).
- `muted`: operator silenced this workflow (noisy or intentionally paused upstream); never alarms until un-muted.

### 6. Surfacing — no span, so route through the errors pipeline

A never-fired workflow has no span to enrich (same constraint as Layer 1's missing node). Emit through `errors.collector.store_error` with:

- `error_type = "Silent failure"` (reuse `health.SILENT_ERROR_TYPE` so it groups with the rest of the class and gets the `SILENT` badge on Overview / Errors / Fleet Health).
- a stable sub-kind `did_not_fire` (parallel to Layer 1's `did_not_run`), so it is countable/filterable apart from in-execution silents.
- `error_summary` like `"scheduled every 1h; last ran 4h 12m ago (expected by 3h ago)"`.
- a synthetic dedup key `heartbeat:{instance_id}:{workflow_id}` (no `trace_id`/`span_id` exists), so `store_error` dedups the outage to one row and `recovered` can resolve it.

`store_error` already broadcasts an `error` event, so the alarm appears live with no new websocket channel. A dedicated `did_not_fire` count can join the Insights `Silent failures` tile.

### 7. The sweep loop

Register one job on the existing scheduler:

```python
scheduler.register(
    "heartbeat_sweep",
    func=heartbeat.sweep,
    interval_fn=lambda: settings.agd_heartbeat_interval_sec,     # default 300
    enabled_fn=lambda: settings.agd_heartbeat_enabled,           # default ON
)
```

`heartbeat.sweep()` per tick:

1. For each `config.get_instances()`:
   - `list_workflows(active=true)` on that instance; if unreachable → one `instance_unreachable`, skip the rest of this instance.
   - For each watched workflow (schedule, or webhook opted-in, or user-interval set):
     - compute `expected_by` from its cadence source + `last_fired_at` (from heartbeat state, seeded from the last-seen span/execution).
     - if `now <= expected_by` → state `ok`, continue (the healthy fast path, no API call).
     - else confirm via `list_executions(workflow_id, limit=1)`; alarm/`did_not_fire` or clear per §3 + §5.

Interval defaults to 5 min. The finest schedule we alarm on is bounded by this interval + grace, which is fine: a workflow that runs every 30s does not need second-accurate outage detection, and sub-interval flapping would be noise.

### 8. Storage

New table `workflow_heartbeat`, keyed `(instance_id, workflow_id)` — durable state the span store can't hold (it is retention-pruned, and the whole point is workflows with *no* recent spans):

| Column | Type | Meaning |
|---|---|---|
| `instance_id` | TEXT | owning instance |
| `workflow_id` | TEXT | n8n workflow id |
| `workflow_name` | TEXT | for the alarm text |
| `trigger_kind` | TEXT | schedule / webhook / user |
| `expected_interval_sec` | INTEGER | derived (A/B) or user-set (C); NULL = not watched |
| `interval_source` | TEXT | schedule / learned / user |
| `last_fired_at` | TEXT | authoritative last execution (from API confirm, else last span) |
| `state` | TEXT | ok / overdue / muted |
| `last_alerted_at` | TEXT | dedup guard for the state machine |
| `updated_at` | TEXT | last sweep touch |

Idempotent `CREATE TABLE IF NOT EXISTS` in `database.py`, same pattern as the `otel_spans` ALTERs.

### 9. Config

```
AGD_HEARTBEAT_ENABLED=true            # master switch for Layer-2 heartbeat
AGD_HEARTBEAT_INTERVAL_SEC=300        # sweep cadence
AGD_HEARTBEAT_MIN_GRACE_SEC=120       # floor on the overdue grace window
AGD_HEARTBEAT_GRACE_FACTOR=0.5        # grace = max(interval*factor, MIN_GRACE)
AGD_HEARTBEAT_LEARNED_ENABLED=false   # Tier B (webhook/learned cadence), opt-in
AGD_HEARTBEAT_LEARNED_SLACK=2.0       # overdue when gap > p95 * slack
AGD_HEARTBEAT_RENOTIFY_SEC=0          # 0 = alarm once per outage; >0 = re-notify egress
```

All env → settings, none hard-coded, matching the Phase 2 knob convention.

## Precision-over-recall, applied

- Tier A (schedule) is deterministic → default on.
- Tier B (learned) is heuristic → default off, opt-in, cold-start silent.
- A missing trigger (no schedule, no opt-in, no user interval) is **not** watched — silence is the safe default, exactly as Layer 1 excludes a missing trigger node ("the workflow never fired, which only an external heartbeat can see").
- Confirm-before-alarm means a telemetry gap never pages; the worst it does is emit an informational `telemetry_gap`.
- Instance-down collapses to one alarm, never a per-workflow storm.

The alarm we ship must stay trustworthy; an over-eager heartbeat that cries "down" every export hiccup gets muted and the whole feature dies. Under-alarm at the margins on purpose.

## Edge cases

- **Multiple schedule triggers on one workflow** → watch the *shortest* interval (soonest expected fire).
- **Schedule changed** (interval edited in n8n) → re-derived each sweep from `get_workflow`, so a widened interval self-corrects; state resets to `ok` if the new window isn't yet overdue.
- **Workflow deleted / deactivated** → falls out of `list_workflows(active=true)`; mark its heartbeat row `muted`/stale, don't alarm.
- **Brand-new active schedule workflow, never run yet** → `last_fired_at` NULL; seed the window from `updated_at`/activation time so we don't instantly alarm a workflow that legitimately hasn't hit its first fire.
- **Instance intentionally stopped** (operator paused it) → surfaces as `instance_unreachable`; acceptable, and the operator can mute the instance. (A future refinement could read AGD's own "instance paused" state to suppress it silently.)
- **DST / cron edge** → handled by evaluating cron from the actual last-fire timestamp rather than assuming a fixed second count.

## Phases

- **Phase A — schedule-derived heartbeat (MVP).** `heartbeat.py` + `workflow_heartbeat` table + scheduler job + schedule-interval parse (extend `_detect_trigger_type`) + confirm-before-alarm + instance-down collapse + `did_not_fire` surfacing + state machine. Covers Tier A + C. This is the 80% case (scheduled integrations are where silent outages hurt most).
- **Phase B — learned cadence for webhooks (Tier B).** Inter-arrival stats + opt-in. Ship after A proves the alarm is quiet in practice.
- **Phase C — egress + self-healing seam.** Reuse the existing notification path for a push on `did_not_fire`; mark the self-healing handoff seam (a never-firing scheduled workflow is a strong repair candidate). Mirrors the parent spec's Phase 4.

## Testing

- Unit: schedule-rule fixtures (fixed interval, cron) → expected interval seconds / next-fire. Cover multi-trigger (shortest wins) and unparseable (falls back to unwatched, not a crash).
- Unit: the state machine — `ok→overdue` fires once, `overdue→overdue` is silent, `overdue→ok` emits `recovered`, `muted` never fires.
- Integration: mock a per-instance client where `list_workflows` returns one hourly-schedule workflow and `list_executions` returns a last run 4h ago → assert one `did_not_fire` with `error_type="Silent failure"`. Then a second sweep with a fresh execution → assert `recovered` and no duplicate alarm.
- Integration (the critical guard): our span store shows no recent trace **but** `list_executions` confirms a run inside the window → assert **no** alarm (telemetry gap, not outage).
- Integration: an unreachable instance with three watched workflows → exactly one `instance_unreachable`, zero `did_not_fire`.
- Reuse the session-scoped-DB trace-id-collision guard noted for Layer 1's tests ([[reference_ageniusdesk_test_harness_gotchas]]).

## Non-goals

- Not editing user workflows or injecting a heartbeat node — detection stays outside the workflow, consistent with the whole feature.
- Not second-accurate outage detection — bounded by the sweep interval by design.
- Not trusting our own telemetry as proof of silence — the n8n API is the ground truth for "did it run."
- Not the repair itself — self-healing is a separate module; Phase C only marks the seam.

## Open questions for review

1. **Cron dependency.** Tier A cron parsing wants `croniter` (or a hand-rolled next-fire for the handful of cron shapes n8n emits). Add the dep, or scope the MVP to fixed-interval schedules only and defer cron? Fixed-interval covers most scheduled workflows; cron is the long tail.
2. **Sweep source of the watched set.** `list_workflows` per instance every 5 min is N API calls per sweep. For a large fleet, cache the workflow list and its parsed cadence, refreshing it on a slower cadence (e.g. every 30 min or on workflow-change events) while the fast sweep only does the cheap time-comparison. Worth building into the MVP, or defer until fleet size demands it?
3. **`last_fired_at` seeding.** Seed from our span store on first run (fast, may be stale), or do one authoritative `list_executions` backfill per watched workflow at startup (accurate, slower cold start)? Leaning span-store seed + lazy confirm.
4. **Learned cadence (Tier B) in or out of the first cut?** Default-off means it ships dark, but it's meaningful surface area. Recommend building A first, landing it, then B.
5. **Instance-paused suppression.** Should `instance_unreachable` check AGD's own "instance stopped by operator" state and stay silent, or always alarm and let the operator mute? Leaning: alarm, because "I thought I paused it but it actually crashed" is a real case.
