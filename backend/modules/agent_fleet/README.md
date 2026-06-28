# Agent Fleet

A managed fleet of LangGraph agents for AgeniusDesk, operated the way AgeniusDesk
operates n8n instances: a catalog, run + a live (polled) graph view, human-in-the-
loop approve/resume, and LangSmith tracing. Adding an agent is adding one `AgentDef`
in `registry.py`; the runner, router, storage, and frontend are all agent-agnostic.

## The v1 agents

- **Ops Triage** (`ops-triage`) — a ReAct tool-loop. Pulls recent errors, inspects
  the failing workflow and its execution payload, checks whether the failure
  recurs, then writes a structured triage. Read-only over the n8n fleet.
- **Fix Proposer** (`fix-proposer`) — investigates, then proposes one minimal,
  reversible fix and HALTS on a LangGraph `interrupt()` backed by a checkpointer.
  You approve, edit, or reject in the UI; the graph resumes from exactly where it
  paused.
- **Health Reporter** (`health-reporter`) — a parallel fan-out / fan-in graph: a
  plan node dispatches three diagnostic lenses that run concurrently in one
  LangGraph superstep, then a synthesize node reconciles them into one report.

## Requirements

This module runs **in_process** and needs two things in the AgeniusDesk
environment:

1. **The langgraph extra.** AgeniusDesk does not pip-install per module, so the
   LangGraph stack must already be installed in the AgeniusDesk environment:

   ```
   langgraph, langchain-core, langchain-anthropic, langchain-openai, langsmith
   ```

   In AgeniusDesk CE these are the `langgraph` optional extra
   (`pip install '.[langgraph]'`); the Docker image installs it. Without it the
   module still loads, but a run reports "LangGraph dependencies not installed".

2. **An Anthropic API key.** The agents run on Claude models. The key is resolved
   from the environment (`ANTHROPIC_API_KEY` / `ANTHROPIC_KEY`), then the AgeniusDesk
   encrypted secret store (`ANTHROPIC_KEY`), then the assistant config if its
   provider is Anthropic. If your AgeniusDesk assistant already uses Anthropic, no
   extra key is needed.

Because it runs in_process (Option A), the module reaches `backend.*` directly for
its n8n read tools and key resolution. The static scanner flags those host imports
HIGH at install; that is accurate and expected for an in_process agent runtime.
Under the container/subprocess isolation tiers the module loads but cannot run a
graph (no host, no tools, no key) — run AgeniusDesk in the in_process tier for this
module, or wait for the consented-secret tier.

## Model overrides

Per-agent model + token overrides via environment variables:

- `OPS_TRIAGE_MODEL`, `FIX_PROPOSER_MODEL`, `HEALTH_REPORTER_MODEL`
- `OPS_TRIAGE_MAX_TOKENS`

Defaults: ops-triage + health-reporter on Haiku, fix-proposer on Sonnet.

## LangSmith tracing

Set `LANGSMITH_TRACING=true` and `LANGSMITH_API_KEY=...` in the AgeniusDesk
environment. Runs then carry a "View trace in LangSmith" link and exact token/cost
figures; without a key the module self-disables tracing and shows native token/cost
estimates instead.

## LangGraph Studio (optional)

`langgraph.json` + `agent/studio.py` let a developer open the graphs in Studio:

```
langgraph dev      # run from this module's directory
```

then open the printed Studio URL. The n8n tools call `backend.*` and only work
inside AgeniusDesk, so in Studio use the graph view to inspect topology and
structure; run the agents for real from the AgeniusDesk dashboard.

## License

MIT. Credit: the LangGraph agent patterns are ported from the AgeniusDesk beta
agent fleet.
