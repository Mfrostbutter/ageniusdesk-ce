# Spec: OpenTelemetry Observability Layer

Status: Draft
Date: 2026-06-26
Owner: Michael Frostbutter
Scope: AgeniusDesk Community Edition (`M:\Code\ageniusdesk-ce`)
Release gate: no (target: next release; larger workstream, may land as its own milestone)
Decision on record: hybrid. Embedded OTLP receiver is the self-contained MVP; a
one-click external observability stack is the documented "go bigger" path.

## 1. Goal

Give operators real, push-based observability into their n8n executions. n8n now
emits OpenTelemetry, so instead of polling the n8n API for coarse after-the-fact
stats, AgeniusDesk can receive per-execution, per-node spans and metrics in real
time and render them. The result is an Observability view that shows *what
happened inside a run* (node-by-node timing, where it failed, how long each step
took), which is a layer deeper than today's Insights.

This is explicitly additive to Insights, not a replacement (see Section 4).

## 2. Non-goals (this spec)

- Becoming a general-purpose APM / tracing backend for arbitrary services. Scope
  is n8n telemetry that AgeniusDesk already has context for.
- Long-term / unbounded trace retention. The embedded store is bounded by age and
  row count; operators who need real retention use the external-stack path.
- gRPC OTLP transport in v1 (HTTP is enough and far simpler in FastAPI; see 5.1).
- Distributed sampling/tail-based sampling logic. v1 takes what n8n sends with a
  simple head cap.

## 3. Current state (analysis)

- `backend/modules/insights/`: analytics for the active instance, derived by
  polling the n8n API (success rates, timelines, busiest/slowest workflows). It is
  pull-based and coarse; there are no node-level spans.
- `backend/modules/docker_mgr/`: one-click container templates and a deployer.
  This is the mechanism the external-stack path reuses.
- `backend/database.py`: the SQLite singleton + idempotent boot migrations.
- `backend/main.py`: middleware stack including the internal-API auth gate and the
  legacy machine-ingest webhook pattern (`AGD_WEBHOOK_TOKEN`, allowlisted paths).
  The OTLP receiver is a machine-ingest endpoint and follows that same pattern.
- WebSocket broadcast bus exists for pushing live updates to the UI.

Gap: nothing ingests OTLP, stores spans/metrics, or renders traces.

## 4. Relationship to Insights (draw the boundary)

| | Insights (exists) | Observability (this spec) |
|---|---|---|
| Source | n8n API, polled | n8n OTLP, pushed |
| Granularity | workflow-level rollups | execution + node-level spans |
| Latency | minutes (poll cadence) | near real-time |
| Question it answers | "how are my workflows doing overall" | "what happened inside this run, and where did it slow down or fail" |

They complement each other. Insights stays as the at-a-glance summary;
Observability is the drill-down. Cross-link them in the UI (an execution in
Insights links to its trace).

## 5. Design (hybrid)

### 5.1 Embedded OTLP receiver (the MVP)

Expose an OTLP/HTTP receiver inside the FastAPI app. n8n exports directly to
AgeniusDesk; no extra infrastructure required.

- Endpoints: `POST /api/otel/v1/traces` and `POST /api/otel/v1/metrics`, accepting
  OTLP/HTTP. Support protobuf (the OTLP default) and JSON encodings.
- Transport: HTTP only in v1. gRPC is deferred (non-goal).
- Parse `ResourceSpans` / `ResourceMetrics`: extract trace_id, span_id, parent,
  name, start/end, status, and attributes; map resource attributes to identify the
  source n8n instance and the workflow/execution/node.
- Auth: this is a machine-ingest endpoint. It is gated by a dedicated
  `AGD_OTEL_TOKEN` (bearer or header), mirroring the `AGD_WEBHOOK_TOKEN` pattern,
  and added to the internal-API gate's machine-ingest branch (not the
  session-authed surface). When unset, document it as open for trusted-LAN only,
  same posture as the legacy webhooks.
- On ingest, broadcast a lightweight "new trace" event over the WebSocket so the
  Observability view updates live.

### 5.2 Storage with bounded retention

- New SQLite tables: `otel_spans` (trace_id, span_id, parent_id, instance_id,
  workflow_id, execution_id, name, kind, start_ns, end_ns, status, attributes_json)
  and `otel_metrics` (name, instance_id, labels_json, value, ts). Indexed on
  trace_id and (instance_id, start).
- Retention is mandatory and bounded both ways: by age (`AGD_OTEL_RETENTION_HOURS`)
  and by row cap (`AGD_OTEL_MAX_SPANS`), pruned on a periodic task. Spans are
  high-volume; this cap is the single most important design control and must exist
  from day one.
- Migrations are idempotent in `_migrate()`.

### 5.3 Observability view (frontend)

A new `frontend/js/views/observability.js`:

- A recent-traces list (one row per execution) scoped to the active instance,
  with status, total duration, and node count.
- A trace waterfall: spans nested by parent/child as horizontal timing bars, so a
  slow or failed node is obvious at a glance. Click a span for its attributes.
- A small metrics strip (throughput, error rate, p50/p95 execution latency) from
  `otel_metrics`.
- Live updates via the WebSocket event from 5.1.
- Linked from Insights (an execution links to its trace) and from the
  Executions/Errors view where a trace exists.

### 5.4 External observability stack (the "go bigger" path)

For operators who outgrow the embedded store, ship a one-click container template
(via the existing docker_mgr template system) that stands up an OpenTelemetry
Collector plus a trace/metric backend (Tempo + Prometheus + Grafana). AgeniusDesk
wires n8n's OTLP export to the collector and links into Grafana from the
Observability view, rather than storing spans itself. This keeps the single-pane
entry point while handing real retention and dashboards to purpose-built tools.

This path is optional and documented; it does not gate the MVP.

## 6. Configuration

| Variable | Default | Purpose |
|---|---|---|
| `AGD_OTEL_ENABLED` | `false` | Turn the embedded receiver on |
| `AGD_OTEL_TOKEN` | (none) | Bearer/header token n8n must send to the receiver |
| `AGD_OTEL_RETENTION_HOURS` | e.g. `72` | Age-based span/metric pruning |
| `AGD_OTEL_MAX_SPANS` | e.g. `500000` | Hard row cap, oldest pruned first |

Document the n8n side: which n8n env vars point its OTLP exporter at
`https://<dashboard>/api/otel` with the token.

## 7. Data and schema changes

- New tables `otel_spans`, `otel_metrics` (Section 5.2), idempotent migrations.
- New module `backend/modules/observability/` (receiver router + ingest + query +
  retention task).
- Internal-API gate: add the OTLP ingest paths to the machine-ingest branch with
  `AGD_OTEL_TOKEN` enforcement.

## 8. Security considerations

- The receiver is unauthenticated-by-default machine ingest, exactly the risk
  class the recent hardening pass addressed. It must support `AGD_OTEL_TOKEN` and
  sit in the machine-ingest allowlist branch, never the session-authed surface,
  and the docs must say "set the token before exposing the receiver publicly."
- Cap request body size (reuse the existing limit) and reject oversized OTLP
  batches.
- Attributes can carry sensitive payload data depending on n8n's instrumentation
  config; document that spans may contain workflow data and are stored at rest in
  SQLite, so the data-volume backup/retention guidance applies.

## 9. Implementation phases

1. OTLP/HTTP receiver (traces) + token auth + body limits.
2. Span storage + bounded retention pruning task.
3. Observability view: traces list + waterfall.
4. Metrics ingest + the metrics strip.
5. Live WebSocket updates + cross-links from Insights/Errors.
6. External-stack one-click template + Grafana linking (optional path).
7. Tests + docs.

## 10. Testing

- Receiver tests: a captured OTLP/HTTP payload (protobuf and JSON) ingests into
  `otel_spans`; a bad token is rejected; oversized batches 413.
- Retention test: pruning enforces both the age and row caps.
- Query test: spans reassemble into a correct parent/child waterfall for one
  trace.
- Run with `uv run pytest`; lint with `uvx ruff check`.

## 11. Open questions

- OTLP encoding priority: protobuf-first (OTLP default) vs JSON-first for
  implementation simplicity. Likely accept both, implement JSON first for testing.
- Traces-only in v1 with metrics in a follow-up, or both together?
- How n8n tags the source instance in resource attributes, and how we map that to
  AgeniusDesk's `instance_id` for multi-instance attribution.
- Retention defaults: tune `AGD_OTEL_RETENTION_HOURS` / `AGD_OTEL_MAX_SPANS` to a
  sane SQLite footprint after measuring real span volume from a busy instance.
- Whether to sample at ingest when over the row cap rather than prune oldest.
