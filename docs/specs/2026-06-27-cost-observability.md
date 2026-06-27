# Spec: Cost Observability (LLM spend folded into the trace layer)

Status: Draft
Date: 2026-06-27
Owner: Michael Frostbutter
Scope: AgeniusDesk Community Edition (`M:\Code\ageniusdesk-ce`)
Release gate: no (extends the OpenTelemetry observability milestone)
Decision on record: cost is a derived dimension over the spans already captured,
not a separate pipeline. Enrich from n8n run-data now; a cost-aware gateway is the
strategic follow-up.

## 1. Goal

Show what each workflow execution *cost* (LLM spend), in the same place operators
already see what it *did* (the Observe trace waterfall). A span tells you what ran;
cost = usage x price. So the work is: get the usage signal, multiply by a price
book, store it on the trace, and surface it in the existing Observe surfaces.

## 2. Key finding (load-bearing; do not re-discover)

Measured against the live fleet (n8n **2.25.6**, captured 2026-06-27):

- **n8n's OTLP spans carry NO token or cost data.** Across a full capture, the LLM
  node span (`@n8n/n8n-nodes-langchain.lmChatAnthropic`) exposes only generic attrs
  (`n8n.node.id/name/type/type_version`, `n8n.node.items.input/output`). The agent
  span (`@n8n/n8n-nodes-langchain.agent`) exposes rich *custom* attrs
  (`n8n.node.custom.ai.agent.tool_calls.*`, `.iteration.count`, `.memory.loads/saves`,
  `.items.total/failed`, `.execution.succeeded`) — useful for agent observability,
  but still **no tokens and no cost**. There are no OpenTelemetry GenAI semantic
  attributes (`gen_ai.usage.*`, `gen_ai.request.model`) at this version.
- **n8n's execution run-data DOES record token usage.** The execution record
  (`execution_data.data`) contains `tokenUsage` with `promptTokens`,
  `completionTokens`, `totalTokens` per AI node. The raw Postgres column is
  "flatted" (index-referenced) JSON; the **n8n API returns it un-flattened**, so go
  through the API, not the DB.

Consequence: cost cannot be derived from n8n OTel spans alone. It is recovered by
enriching each trace from the execution it already references (we store
`execution_id` on every span).

## 3. Current state

- `backend/modules/observability/` (shipped, OTel Phase 1+3): OTLP receiver,
  `otel_spans` store with bounded retention, query API, the Observe view (traces
  list + waterfall), and the per-execution popup. No cost anywhere.
- `backend/modules/assistant/providers.py`: AgeniusDesk's own LLM calls (Ask AI /
  triage / Code Lab) resolve a provider + model and could report usage directly.
- `n8n_proxy/`: an authenticated client to each connected instance's n8n API,
  including a stored per-instance API key — the credential the enrichment reuses.

## 4. Design

### 4.1 Usage source (the bridge)

Primary, available today: **enrich from n8n run-data.**

- When a trace contains AI nodes, fetch the execution from the connected instance:
  `GET /executions/{execution_id}?includeData=true` via the existing `n8n_proxy`
  client (reuses the instance's stored API key; the API returns clean JSON).
- Parse, per AI node: `tokenUsage.{promptTokens, completionTokens, totalTokens}`
  and the resolved model (from the node's run-data / parameters).
- Enrichment is keyed by `execution_id`, which every captured span already carries
  via the root `workflow.execute` span.

### 4.2 Price book (staying current and accurate)

A model -> price map `{model: {input_per_mtok, output_per_mtok}}`. Prices drift, so
the book is self-refreshing, layered, and snapshotted at compute time rather than
hand-maintained.

**Source of truth: OpenRouter's models API.** `GET https://openrouter.ai/api/v1/models`
is public (no key) and returns ~hundreds of models across every provider with
`pricing.prompt` / `pricing.completion` as per-token USD. We pull from it instead of
curating prices by hand.

**Layered resolution (highest wins):**
1. Operator override (manual UI edit — private/self-hosted models, or to pin a number).
2. Auto-fetched OpenRouter snapshot.
3. Bundled default snapshot (shipped in the image so a fresh / offline / air-gapped
   install still prices common models on day one).

All three live in `data/price_book.json` (fetched + overrides) plus the bundled
default; resolution is override > fetched > bundled.

**Refresh:** a scheduled background fetch on a TTL (`AGD_PRICEBOOK_REFRESH_HOURS`,
default 24) pulls `/models`, parses pricing, rewrites the cache. Fail-safe: any fetch
error keeps the last-good cache and never blocks enrichment. Per model we store
`source` + `priced_at`; the UI shows "priced from OpenRouter, updated Nh ago", a
**Refresh now** button, and a staleness flag past a threshold.

**Compute-time snapshot (do not retro-reprice).** Cost is computed when a trace is
enriched, using the price *then in effect*, and the realized `cost_usd` **and the
unit prices used** are stored on the span (Section 4.3). Later price changes affect
only new enrichments; historical traces keep their real cost.

**True cost where it is free.** If a call actually routed through OpenRouter, its
response carries real cost — use that, not the book. The book is for direct-provider
calls (e.g. `lmChatAnthropic` -> Anthropic), where OpenRouter's resale price is a
close approximation, not always penny-identical; the operator override covers
exactness.

**Model-id matching.** n8n run-data gives a model id (e.g. `claude-sonnet-4-...`)
that may not match OpenRouter's namespaced id (`anthropic/claude-sonnet-4`). A small
normalize/alias step maps them; an unmatched model stores tokens with `cost_usd=null`
and surfaces "tokens known, price unknown" so the operator can map it once.

Formula: `cost_usd = prompt_tokens/1e6 * input_per_mtok + completion_tokens/1e6 * output_per_mtok`.

### 4.3 Storage

Add to `otel_spans` (or a sibling `otel_costs` keyed by span_id) the columns:
`model TEXT`, `tokens_in INTEGER`, `tokens_out INTEGER`, `cost_usd REAL`,
`cost_source TEXT` (`n8n-rundata` | `gateway` | `agd-assistant`), and the
compute-time price snapshot `price_in_per_mtok REAL`, `price_out_per_mtok REAL`,
`price_source TEXT`, `priced_at TEXT` (so a stored cost is auditable and never
retro-repriced). Idempotent migration in `_migrate()`. Cost then rolls up per trace,
workflow, model, instance with plain SQL — no second store.

### 4.4 AgeniusDesk's own assistant spend (free add-on)

`providers.chat()` already knows the model and receives provider usage. Emit an
internal span per assistant call into the same `otel_spans` store with
`cost_source='agd-assistant'`, so the dashboard's own AI cost shows alongside n8n's.

### 4.5 Strategic option (later): cost-aware gateway

Route LLM calls through a cost-aware gateway (LiteLLM / OpenRouter proxy) that emits
`gen_ai` OTLP spans with real provider cost to the same receiver. n8n's
`N8N_OTEL_TRACES_INJECT_OUTBOUND=true` propagates trace context on outbound calls,
so a gateway span nests under the originating node span. Captures all LLM spend,
including calls made outside n8n, with provider-true cost. Deferred: it is infra
(a gateway) and depends on calls actually traversing HTTP the gateway fronts.

## 5. UI (same Observe surfaces)

- Trace waterfall: a `$` badge on AI spans and a **total cost** for the trace; also
  surface the agent custom attrs (tool calls, iterations, memory) in the span detail.
- Traces list: a **cost** column per execution.
- A **spend rollup**: by workflow, by model, over time (the at-a-glance cost view).
- Cross-link from Insights/Errors as today.

## 6. Relationship to the roadmap

This **folds the Medium-Term "Cost tracking integration" item into the Observability
milestone**. Cost is the spend dimension of the trace store, not a standalone
feature.

## 7. Implementation phases

1. Price book: bundled default snapshot + `data/price_book.json` (layered
   override > fetched > bundled) + the scheduled OpenRouter `/models` refresh task
   (`AGD_PRICEBOOK_REFRESH_HOURS`, last-good fallback) + operator edit; and the
   `otel_spans` cost + price-snapshot columns + migration.
2. Run-data enrichment: detect AI-node traces, fetch the execution via `n8n_proxy`,
   parse `tokenUsage` + model, compute and store cost. Throttled / idempotent per
   execution.
3. UI: cost badges + trace total + traces-list cost column.
4. Spend rollup view (by workflow / model / time) + agent-attr surfacing.
5. AgeniusDesk assistant self-instrumentation (`cost_source='agd-assistant'`).
6. (Later) cost-aware gateway emitting `gen_ai` spans.
7. Tests + docs.

## 8. Testing

- Enrichment unit test against a captured n8n execution JSON fixture: parse
  `tokenUsage` + model for each AI node; price-book math; unknown-model path.
- Idempotency: re-enriching an execution does not double-count.
- Rollup query test: per-workflow / per-model sums reconcile with per-span costs.
- Run with `uv run pytest`; lint touched files with `uvx ruff check`.

## 9. Open questions

- Enrichment timing: eager on ingest for AI-node traces (drives the rollup) vs lazy
  on trace open (cheaper). Likely eager-but-throttled, with a lazy backfill.
- Model resolution: confirm the model id is reliably in run-data for every AI node
  type (lmChat*, agent, tool sub-nodes), or whether some require the node params.
- Whether to also estimate non-LLM cost (compute time) — out of scope for v1.
- Whether a newer n8n adds `gen_ai` span attrs (revisit; would make 4.1 a pure
  ingest read and retire the enrichment call).
