# Model Cost module + dashboard card contribution

Date: 2026-09-07
Status: building

## Goal

A standalone, distributable community module that gives **per-model AI cost
observability** inside AgeniusDesk: which models are costing what, across
Anthropic, OpenAI, and OpenRouter. It surfaces in its **own module view** and as
a **pinnable card on the Main Dashboard**. Self-contained (bring-your-own admin
keys); no external appliance. TokenPulse (Mfrostbutter/tokenpulse) is the
reference implementation the provider logic is ported from, not a runtime
dependency.

## Two pieces

### 1. Host investment: dashboard card contribution (ageniusdesk-ce)

The Main Dashboard's widgets are a host-side registry (`frontend/js/views/dashboard.js`)
with add/remove, drag-to-reorder, and per-dashboard localStorage. Today the
registry is hardcoded. A module can now contribute pinnable cards.

- Manifest: `ContributesDecl.dashboard_cards: list[DashboardCardDecl]`, each
  `{id, title, size: "half"|"full", data}` where `data` is a subpath under the
  module's API prefix returning the card JSON.
- Card JSON contract (module serves it):
  `{title?, metrics: [{label, value, sub?}], rows?: [{label, value, sub?}], link?, footer?, updated_at?}`.
- `dashboard.js` reads `/api/modules`, extracts each loaded module's
  `contributes.dashboard_cards`, and registers each as a widget
  (`module:{module_id}:{card_id}`) in the widget registry and the "+ Widget"
  modal. It renders host-side by fetching `data` (browser session authorizes the
  read via the identity middleware) and drawing a generic card. Degrade, never
  fatal: a card whose data call fails shows an error line, the dashboard is fine.
- No new host backend endpoint: the catalog comes from the existing
  `/api/modules`, the data from the module's own route. Model JS in the host page
  is avoided; the card is host-rendered from JSON (same posture as the Fleet
  Health contribution).

### 2. The module: `model-cost` (ageniusdesk-community-modules)

Files mirror the Proxmox module (the reference for a bridge-backed module).

- `manifest.json`: three `host.http` endpoints, auth injected host-side:
  - `anthropic`  base `https://api.anthropic.com`, header `x-api-key: {value}`, secret `ANTHROPIC_ADMIN_KEY`, GET only.
  - `openai`     base `https://api.openai.com`, header `Authorization: Bearer {value}`, secret `OPENAI_ADMIN_KEY`, GET only.
  - `openrouter` base `https://openrouter.ai/api/v1`, header `Authorization: Bearer {value}`, secret `OPENROUTER_KEY`, GET only.
  - `contributes`: `fleet_health: "fleet-health"` and one `dashboard_cards` entry.
  - Every secret optional at the module level: a provider with no key is simply
    absent (`unconfigured`), the others still work.
- `_host.py`: the Proxmox facade verbatim, `MODULE_ID = "model-cost"`.
- `prices.py`: per-million-token price table by model family (ported/extended
  from TokenPulse's Claude Code prices, plus common OpenAI models). Unknown model
  = zero derived cost, never a guess.
- `client.py`: per-provider poll through the bridge, degrade-not-fatal, short-TTL
  cache. Normalizes to model rows and provider summaries.
- `router.py`: `GET /summary?window=`, `GET /card`, `GET /fleet-health`,
  `GET /settings`, `POST /refresh` (operator).
- `static/model-cost.html` + `static/module.js`: the rich own view (per-model
  table, window tabs, per-provider actual-vs-estimated reconciliation).

## Data model (normalized)

Per-model row:
`{source, model, window, input_tokens, output_tokens, tokens, cost, cost_basis}`
where `cost_basis` is `"actual"` (OpenRouter per-model spend) or `"estimated"`
(Anthropic/OpenAI, derived tokens x price table).

Provider summary:
`{id, name, reachable, error, currency, actual_cost: {today, mtd}, estimated_cost, tokens, models}`.

## Cost honesty (the load-bearing constraint)

The provider **cost** endpoints report a total, not a per-model breakdown; only
their **usage** endpoints break down by model (tokens). OpenRouter's `/activity`
is the exception: real per-model spend for the last 30 completed UTC days.

So:
- OpenRouter per-model cost = **actual** (30d window).
- Anthropic / OpenAI per-model cost = **estimated** (tokens x prices), for today
  and MTD, and always labelled estimated. Each provider's **actual** total cost
  (from its cost endpoint) is shown alongside so drift between the estimate sum
  and the real total is visible, never hidden.

Windows differ by provider (Anthropic/OpenAI: today + mtd; OpenRouter: 30d). The
UI tabs by window and shows only providers with data for that window.

## Provider endpoints (ported from TokenPulse)

- Anthropic: `GET /v1/organizations/cost_report` (total, cents-as-string) +
  `GET /v1/organizations/usage_report/messages?group_by[]=model` (per-model tokens).
  Header `anthropic-version: 2023-06-01` (module sets it; not the injected auth header).
- OpenAI: `GET /v1/organization/costs` (total, dollars under amount.value) +
  `GET /v1/organization/usage/completions?group_by[]=model` (per-model tokens).
- OpenRouter: `GET /v1/activity` (per-model actual spend, 30d) + `GET /v1/credits`
  (balance) + `GET /v1/key` (limit/remaining) — all with the same key.

## Out of scope for v1

Claude Code local (reads local disk logs; does not generalize to a distributable
module). Budgets/quota gauges (TokenPulse's job). Historical persistence beyond
the short poll cache (add a store table later if trends are wanted on the card).
