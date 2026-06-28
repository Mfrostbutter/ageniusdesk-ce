# Agent Fleet

AgeniusDesk operates AI agents the way it operates n8n: you **build** them in Code
Lab and **run + monitor** them in the Agent Fleet. Agents are real
[LangGraph](https://langchain-ai.github.io/langgraph/) or
[PydanticAI](https://ai.pydantic.dev/) automations, not n8n-ops helpers, so an
agent can do whatever you write it to do. Three are bundled as examples; the rest
you build yourself.

## Prerequisites

Agent Fleet needs two things in the AgeniusDesk environment:

1. **The agent dependency extra.** The LangGraph / PydanticAI stack is opt-in so
   the default image stays lean. Build the image with the extra:

   ```bash
   AGD_EXTRAS="assistant,langgraph" docker compose up -d --build
   ```

   (or `pip install '.[langgraph]'` on bare metal). Without it the **Agent Fleet**
   view still loads, but a run reports the missing extra and Code Lab's Agent
   Builder shows an "install the extra" nudge.

2. **An Anthropic API key.** The built-in agents run on Claude. The key resolves
   from the environment (`ANTHROPIC_API_KEY` / `ANTHROPIC_KEY`) or the encrypted
   Secrets store (`ANTHROPIC_KEY`); if your AI Assistant already uses Anthropic, no
   extra key is needed. Optional: set `LANGSMITH_TRACING=true` + `LANGSMITH_API_KEY`
   to trace runs in LangSmith.

## The built-in agents

Open **Agent Fleet** in the sidebar. Out of the box:

- **Ops Triage** — a ReAct tool-loop: pulls a live n8n error, inspects the failing
  workflow and execution, checks whether it recurs, and writes a root-cause triage.
  Read-only.
- **Fix Proposer** — investigates, proposes one minimal, reversible fix, and
  **pauses for your approval** (a LangGraph `interrupt()`); approve, edit, or
  reject, and the graph resumes from where it paused.
- **Health Reporter** — a parallel fan-out: three diagnostic lenses run at once,
  then a synthesize step reconciles them into a fleet health report.

## Running an agent

1. Pick an agent card.
2. Choose what to act on (an error from the picker, or a free-form prompt), or
   leave it blank to use the most recent error.
3. Click **Run agent**.

Each run streams live:

- **Live graph** (LangGraph agents) — the node graph lights up as the run moves
  through it.
- **Run waterfall** — a normalized, framework-agnostic timeline of every step
  (reasoning, tool calls, durations). Expand **Step detail** for the full tool
  arguments, results, and reasoning text. The waterfall looks the same for
  LangGraph and PydanticAI, so monitoring is consistent.
- **Result** — the agent's final output, rendered as markdown, with a token + cost
  chip and (when tracing is on) a LangSmith trace link.

Human-in-the-loop agents (like Fix Proposer) stop at **Awaiting your approval**;
approve, edit, or reject to resume. Past runs replay their full timeline on click.

## Building your own agent

Build agents in **Code Lab → Agent Builder** (see the [Code Lab guide](code-lab.md)):

1. Switch the mode toggle to **Agent Builder**.
2. Pick a **framework** (LangGraph or PydanticAI) and a **starter** (ReAct,
   human-in-the-loop, parallel fan-out, or blank). The editor loads a scaffold.
3. Write the agent. The AI sidebar is agent-aware and will help. A LangGraph agent
   is a pure factory — `build(llm, tools, checkpointer=None)` returning a compiled
   graph — that imports only langgraph/langchain; AgeniusDesk injects the model and
   the tools you pick, so the agent code never touches host internals.
4. Click **Register to Agent Fleet**. Name it, pick its model, select the tools it
   may call, and toggle human-in-the-loop. It is saved into your vault and appears
   in the Agent Fleet immediately.

## Agents live in your vault

A registered agent is a folder in your workspace vault under `agents/<id>/`:

- `agent.json` — the manifest (name, framework, model, tools, hitl, badges, ...).
- `graph.py` — the agent code (a pure `build(...)` factory; optional
  `initial_state(task, target)` and `kickoff(error_id, prompt)` overrides).

Because they are plain files you own, you can edit them in any editor (or Code
Lab), version them, export them, or delete them. The fleet **discovers** vault
agents on every read, so changes show up with no restart, and a run picks up the
latest code automatically.

To remove an agent from the dashboard, expand its card in the Agent Fleet and
click **Delete**; it deletes the `agents/<id>/` folder from your vault after a
confirm. The three built-in example agents have no Delete button and cannot be
removed. Deleting is blocked while a run is in progress. Any secrets the agent
used are left in the Secrets store for you to clean up separately.

## LangGraph vs PydanticAI

Both run and render in the same waterfall. Today:

- **LangGraph** agents get the live node graph in addition to the waterfall, and
  the host injects the tools you select.
- **PydanticAI** agents run through an adapter (`agent.run()`); define their tools
  in the agent code. The graph panel is LangGraph-only.

## LangGraph Studio (optional)

For LangGraph development, `langgraph.json` is wired so you can run `langgraph dev`
and open the graphs in Studio; the **Open in Studio** link in the Agent Fleet
points at a local dev server. The in-app view is the primary live view.
