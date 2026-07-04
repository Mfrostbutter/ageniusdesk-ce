# Spec: Local-Model Cost Clarity (Ollama / self-hosted Custom endpoint)

Status: Draft
Date: 2026-07-02
Owner: Michael Frostbutter
Scope: AgeniusDesk Community Edition
Release gate: no (small follow-up to the shipped cost-observability milestone)
Decision on record: token capture for local models is not broken and needs no new
plumbing. The gap is a labeling problem in the price book and the waterfall, not a
missing-data problem.

## 1. Goal

Stop the waterfall from showing "price unknown" for Ollama and self-hosted Custom
(OpenAI-compatible) models. Those calls cost $0 by construction (no metered API
behind them); the UI should say so, not lump them in with cloud models the price
book genuinely has no rate for.

## 2. Key finding (load-bearing; do not re-discover)

Token capture already works for every provider today, because n8n's run-data
`tokenUsage` (`ai_languageModel[0][0].json.tokenUsage`) is populated the same way
regardless of which AI node produced it (`backend/modules/observability/cost.py:34-39`).
`enrich_trace()` writes `tokens_in`/`tokens_out` to the span unconditionally
(`cost.py:96-108`); pricing is a separate, independent step (`cost.py:89-94`) that
only affects `cost_usd` and `price_source`.

The actual gap: `pricing.price_for(model)` (`pricing.py:97-118`) checks operator
overrides, the OpenRouter-fetched table, then the bundled defaults, in that order,
and returns `None` if nothing matches. Ollama models (e.g. `llama3.1:8b`) and
arbitrary Custom-endpoint models are never in any of those three tables, so they
always fall through to `None`. `cost.py:94` then sets `price_source = "unknown"`,
and the waterfall renders `price unknown` (`frontend/js/components/trace-waterfall.js:102`)
identically to a cloud model the price book simply hasn't caught up to yet.
Operators can't tell "this is free and local" from "we don't know this model's
list price" without opening the node and checking which provider it uses.

Provider identity is already on the span and doesn't need to be inferred from the
model string: n8n's OTel export attaches `n8n.node.type` per AI node (e.g.
`@n8n/n8n-nodes-langchain.lmChatOllama` vs `lmChatOpenAi`), and `ingest.py:106-126`
already persists the full attribute set into `attributes_json` on every span. That
attribute just isn't read anywhere in the cost path today (`cost.py` only reads
`n8n.node.name` at line 70).

## 3. Design

### 3.1 Detecting a local model

Prefer node-type detection over model-name heuristics (guessing from a model
string like `llama3.1` is fragile and doesn't cover Custom endpoints pointed at
`localhost`/a LAN IP). In `enrich_trace()`, alongside `_model_for(run)`, read
`n8n.node.type` off the matched span's attributes:

- `@n8n/n8n-nodes-langchain.lmChatOllama` / `lmOllama` -> always local.
- `@n8n/n8n-nodes-langchain.lmChatOpenAi` (Custom/OpenAI-compatible mode) with a
  base URL resolving to a private address (loopback / RFC1918 / `.local`) ->
  local. Base URL isn't on the span today; pull it from the node's credential/
  parameters the same way `_model_for` already reaches into run-data, or accept
  the coarser "Custom provider, no OpenRouter/bundled match" as good-enough
  signal for v1 and defer base-URL sniffing.

### 3.2 Pricing

Add a fourth, lowest-priority tier to `pricing.price_for()`: if no override/
OpenRouter/bundled match and the caller signals "local", return
`{"in": 0.0, "out": 0.0, "source": "local", "estimate": False}` instead of `None`.
This is the one case where `estimate` is legitimately `False` — a local model's
cost isn't an approximation, it's exactly zero. Requires threading the
local-detection result from `cost.py` into `price_for()` (new optional param, e.g.
`price_for(model, is_local=False)`), since pricing.py currently only ever sees the
model string.

### 3.3 Storage

No schema change. `price_source` already accepts arbitrary text (`"local"` joins
the existing `override`/`openrouter`/`bundled`/`unknown` values); `cost_is_estimate`
is written from the resolved `pr["estimate"]` and becomes `0` for this tier.

### 3.4 UI

`trace-waterfall.js:101-102`: when `price_source === "local"`, render
`$0.00 (local)` instead of `fmtUsd(cost_usd)` with the `(est)` suffix, so a $0
local call reads distinctly from a genuinely-estimated cloud call. Token counts
(`in X / out Y tok`) render unchanged either way — they're not gated on pricing.

## 4. Non-goals

- Compute/GPU-time cost estimation for local inference (out of scope; this spec
  is about not mislabeling $0 as "unknown", not about modeling local hardware cost).
- Base-URL sniffing for every possible self-hosted OpenAI-compatible target beyond
  Ollama in v1 — start with the node-type signal, revisit if Custom-endpoint local
  usage turns out to be common enough to justify parameter inspection.

## 5. Implementation phases

1. `cost.py`: read `n8n.node.type` from the matched span's attributes alongside
   the existing `n8n.node.name` lookup; classify Ollama node types as local.
2. `pricing.py`: add the `local` tier to `price_for()` (`in`/`out` = 0,
   `estimate = False`), gated on the caller-supplied local flag.
3. `trace-waterfall.js`: render the `local` `price_source` distinctly.
4. Tests: unit test for the local-tier price resolution; enrichment test with an
   Ollama-node fixture asserting `cost_usd == 0.0`, `price_source == "local"`,
   `cost_is_estimate == 0`, and non-null token counts.

## 6. Relationship to the roadmap

Near-Term item **"Local-model cost clarity"** in `ROADMAP.md`.

## 7. Open questions

- Whether to extend local-detection to the Custom (OpenAI-compatible) provider via
  base-URL inspection in v1, or defer it (see Non-goals) until there's a concrete
  case of a self-hosted Custom endpoint being mislabeled.
- Whether operators would ever want to override a "local" classification back to a
  real price (e.g. a self-hosted model billed by GPU-hour elsewhere) — likely yes,
  and the existing override tier already outranks every other tier including
  `local`, so no new mechanism is needed, just confirm the resolution order holds.
