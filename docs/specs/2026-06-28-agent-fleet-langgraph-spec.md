# Spec: Agent Fleet (core module, LangGraph + PydanticAI)

Status: SPEC / approved. **Distribution decided 2026-06-28: Agent Fleet ships as a
CORE built-in module, not a community module** (see "Distribution: core, not
community" at the end). The LangGraph/PydanticAI deps stay opt-in via the
`langgraph` extra with graceful degradation. This reverses the roadmap's "all three
integration modules are community" lock for Agent Fleet only (Proxmox + secret
backends stay community). The credential fork below is now moot: a core module runs
in the host process by definition.

The sections up to "Authoring + management direction" describe the original v1
community-module port (historical, already built + dogfooded); they are superseded
on distribution + authoring by the two dated decision sections at the end.

Date: 2026-06-28

## What this is

A community module (`modules/agent-fleet/`, installed opt-in via the
inspect/scan/consent pipeline) that runs a managed fleet of LangGraph agents:
a catalog, run + live-stream, and human-in-the-loop approve/resume, with a live
node-by-node graph view and LangSmith tracing. It ports our internal LangGraph
agent fleet (the runner, registry, router, the live graph view, HITL resume,
LangSmith capture) into CE as the first agent runtime.

The runtime is agent-agnostic: adding an agent is adding one `AgentDef`. v1 ships
the LangGraph adapter only; the run-event contract is defined so a second adapter
(PydanticAI) emits the same events later without frontend work.

## What the investigation changed vs the roadmap

The roadmap assumed the only host secret is the LLM key, already handled by the
bridge's `assistant.complete`, so there was "no credential-into-worker problem."
That is wrong for this module. Two reasons, both load-bearing:

1. `assistant.complete` is **one-shot and tool-free** by construction (the host
   completion sends no `tools` field, deliberately, so a module cannot reach
   mutating tools through a "completion"). The fleet's flagship pattern is the
   ReAct tool-loop, which needs provider-native `llm.bind_tools(...)`. The bridge
   cannot provide that.
2. **LangSmith tracing** (a hard requirement: "traceable exactly as today")
   captures LLM spans from inside the langchain runtime in the process that makes
   the call. Route the LLM through the bridge to the host and the worker's
   langchain context never sees the generation, so the LLM spans vanish from the
   trace. To trace as today, the keyed ChatModel must run where the graph runs.

Conclusion: a faithful port needs the LLM key (and the LangSmith key) present in
the process running the graph. That is the consented-secret posture the roadmap
said this module would avoid. It does not avoid it. See "The credential fork."

## Transport correction (post-investigation)

The spec originally planned a per-run SSE endpoint. That does NOT work for a
community module's frontend: the view runs in a sandboxed, opaque-origin iframe
whose only host channel is `window.AgeniusDesk.fetch`, which is **fully buffered**
host-side (`await r.text()`), exposes no `body`/`getReader()`, and cannot
authenticate a native `EventSource` (no session cookie at the null origin). The
reverse-proxy SSE streaming is real but only applies to the worker<->host backend
hop under isolation, not to the iframe. So:

- **Frontend transport is POLLING**, the established CE community pattern
  (youtube-research polls). The runner persists the growing event log to storage
  on every emit (the beta already does this); the frontend polls
  `GET /api/agent-fleet/runs/{id}` every ~1.5s while a run is running or paused.
- **No SSE endpoint and no `/ws` broadcast.** The runner drops the
  `backend.websocket` broadcast entirely (the iframe could not hear it anyway).
- The SVG graph view renders idempotently from the full event log, so polling
  lights up nodes as events accumulate (~1.5s granularity). It ports unchanged.
- The live graph hard requirement is met (polled-live); true token streaming is
  not available to the iframe and is out of scope.

## What the investigation confirmed is free

- **HITL + LangSmith.** Run in_process, so both work natively (host keys, the
  whole runtime in one process). The graph view ports unchanged.
- **HITL parking.** The whole runtime (runner + graphs + checkpointer) runs in
  ONE worker process. The beta parks the live compiled graph + an in-memory
  `MemorySaver` in module globals; that works inside a single long-lived worker.
  Same limitation as the beta (parked runs do not survive a process restart),
  acceptable for v1. No cross-process checkpointer needed for v1.
- **The bridge stays the boundary for everything except the consented secrets.**
  Notes/memory go through the existing `_host` facade (`notes_*`). Only the LLM +
  LangSmith + n8n-API credentials are the consented exception.

## v1 agent set

The internal fleet has many agents; most are coupled to private infrastructure
(message lists, social, content publish, answer-engine probes) and are out of
scope for CE. v1 ships the three that are clean, integration-free, and together
exercise every runtime feature:

- **ops-triage** — ReAct tool-loop. Investigates a live n8n failure and writes a
  root-cause triage. Read-only over the n8n fleet. Demonstrates the tool-loop +
  the live graph view.
- **fix-proposer** — single HITL gate. Proposes a fix, `interrupt()`s for
  approve/edit/reject, resumes. Demonstrates interrupt/resume + the approval UI.
- **health-reporter** — parallel fan-out / reduce. A plan node dispatches N
  lenses in one superstep, a synthesize node reduces. Demonstrates the parallel
  pattern + an additive state channel.

These three cover ReAct, HITL, and parallel fan-out, plus topology, streaming,
LangSmith, and token/cost capture. The content/publish/remediation agents are
explicitly out of CE v1 (private-infra coupling + external-write risk).

## n8n access from the worker (the tools)

The beta agents reach n8n through in-process host internals. A worker cannot
import `backend.*`, so v1 uses the HTTP toolset pattern instead: the agent tools
call **CE's own public API** (`/api/v1`, X-API-Key auth) over the worker's egress
network. This reuses the `public_api` module, needs no new bridge surface, and
mirrors how the beta already drives the same graphs from Studio over HTTP.

Worker needs: `AGD_BASE_URL` (the host, reachable by container name / host alias)
and an `AGD_API_KEY` (a consented worker secret). Build-time check: confirm
`public_api` exposes the n8n read endpoints ops-triage needs (list/inspect
workflows, executions, errors); add the read endpoints if any are missing
(read-only additions to public_api, MIT).

## The run-event contract (agent:run)

The SSE stream is the adapter contract. Each event is one JSON object on a
`text/event-stream`, carrying `run_id` and a `phase` discriminator. Phases and
payload keys (ported verbatim from the beta so the existing frontend consumes it
unchanged):

- `started` — `{run_id, phase, task, model, agent_id, agent_name}`
- `thinking` — `{run_id, phase, node, text}`
- `tool_call` — `{run_id, phase, node, tool, args}`
- `tool_result` — `{run_id, phase, node, tool, preview}` (preview truncated)
- `node` — `{run_id, phase, node, label, text}` (named timeline step)
- `node_light` — `{run_id, phase, node}` (lights the graph node, no timeline text)
- `awaiting_approval` — `{run_id, phase, proposal_md, choices?}`
- `resumed` — `{run_id, phase, action}`
- `final` — `{run_id, phase, triage_md, trace_url, total_tokens, total_cost, usage_detail}`
- `error` — `{run_id, phase, message}`

A PydanticAI adapter (later) emits the same phases; the rich graph view is
LangGraph-first, PydanticAI gets run/step streaming on the same contract.

## Module layout (`modules/agent-fleet/`)

```
manifest.json        id agent-fleet; capabilities (network egress, host.assistant,
                     filesystem write_paths, the consented secrets); secrets_required
__init__.py          re-exports router (lazy, so Studio import does not drag the app)
router.py            APIRouter(prefix="/api/agent-fleet"); _host.ISOLATED auth pattern;
                     GET agents, GET agents/{id}/graph (topology), POST triage,
                     POST runs/{id}/resume, GET runs, GET/DELETE runs/{id}
runner.py            agent-agnostic driver; persist-only emit (frontend polls);
                     HITL park/resume; LangSmith capture; native token/cost
registry.py          AgentDef catalog + the 3 v1 builders + key resolution
storage.py           run persistence in a MODULE-OWNED data/agentfleet.db
                     (CE's _migrate drops langgraph_runs from dashboard.db, so the
                     module never touches the shared DB) — mirrors notes/index.py
_host.py             dual-mode facade (notes_* via bridge or direct) [from youtube-research]
tools_local.py       n8n/errors read tools calling CE backend directly (in_process)
agent/
  state.py prompts.py
  graph.py           ops-triage (ReAct)
  graph_hitl.py      fix-proposer (single HITL gate)
  graph_health.py    health-reporter (parallel fan-out)
  studio.py          langgraph dev / Studio entrypoint (best-effort; in-app view is primary)
static/
  agent-fleet.html   the catalog + run composer + timeline view (fragment)
  agent-fleet.js     ported langgraph.js: polls via AgeniusDesk.fetch, no WS/SSE
  graph.js           ported langgraph-graph.js (SVG live view) UNCHANGED
langgraph.json       Studio manifest (graphs -> studio entrypoints)
README.md  tests/
```

Port effort: ~8k LOC in the beta, but v1 drops the private-infra agents. Clean
copy: registry dataclass, the 3 graphs, state/prompts, storage shape, the SVG
graph view (`graph.js`). Adaptation: runner broadcast -> SSE; frontend
subscription -> per-run EventSource + open/close lifecycle; key resolution ->
consented worker env (no `backend.config` internal); tools -> HTTP over /api/v1.

## Dependencies under isolation

The langgraph extra (`langgraph==1.2.4`, `langchain-core==1.4.6`,
`langchain-anthropic==1.4.5`, `langchain-openai>=1,<2`, `langsmith==0.8.14`) must
be importable in the worker. Under container isolation the worker runs the
dashboard image with a read-only rootfs, so deps cannot be pip-installed at
runtime. Options:

- A. Bake an optional extra into the CE image (build arg / image variant);
  agent-fleet requires the operator run that image. Cleanest for container tier.
- B. in_process tier with the host venv carrying the extra (`uv sync --extra
  agent-fleet`). Works today, no image work.

This ties directly to the credential fork below.

## The credential fork (the one open decision)

agent-fleet needs the LLM + LangSmith + n8n-API credentials in the process that
runs the graph. Two shapes:

### Option A: in_process v1, consented-secret tier as the documented follow-on
Ship agent-fleet running in the in_process tier first. It uses the host's already
resolved keys directly, gets native LangSmith, native tool-loops, native token
streaming, and SSE through the in-process path. Fastest faithful port; proves the
agent:run contract + live view + LangSmith end to end. Then spec/build the
consented-secret injection (below) as the immediate hardening that lets it run
sandboxed.

Cost: agent-fleet does not run under container isolation until the follow-on
lands; on a host set to global container isolation it must run in_process (a
per-module tier override, or operator runs the host in_process).

### Option B: build the consented-secret tier first, ship sandboxed from day one
Build the minimal host extension now: the manifest declares which
`secrets_required` must be injected INTO the worker (a new `in_worker: true` flag
or a `capabilities.worker_secrets` list); the AST scanner flags it HIGH ("this
module receives these secrets in its sandbox"); the operator consents at install;
the subprocess/container env builders inject ONLY those declared+consented
secrets (resolved host-side) into the worker, everything else stays scrubbed.
Then agent-fleet runs fully sandboxed (read-only fs, no docker socket, egress
network) with the keys scoped to it. Also requires the langgraph extra baked into
the image (Option A above).

Cost: meaningfully more host work (manifest schema + scanner finding + consent
gate + both env builders + image extra) before the fleet is dogfoodable.

### Decision: Option A (chosen 2026-06-28)
agent-fleet is first-party MIT code and v1 agents are first-party `AgentDef`s, so
the in_process sandbox gap buys little for v1, and we get a working, traceable,
live-streaming fleet fastest. The consented-secret injection is genuinely useful
(it is also what Proxmox and any future keyed module need), so it is the next
shared capability AFTER the fleet, not a blocker for it. Step 9 (the host
extension) is deferred to that follow-on.

## Build order (once the fork is decided)

1. Scaffold `modules/agent-fleet/` (manifest, __init__, _host from youtube-research).
2. Port registry + the 3 graphs + state/prompts; LLM construction from env keys.
3. Port runner with the SSE per-run queue; define the agent:run emitter.
4. Port router endpoints incl. SSE stream + topology + resume.
5. tools_http over CE /api/v1; confirm/extend public_api n8n read endpoints.
6. Port the frontend: catalog/composer/timeline (per-run EventSource) + the SVG
   graph view (unchanged); wire `frontend.nav`.
7. Studio wiring (langgraph.json + studio.py) for `langgraph dev`.
8. Tests: headless graph drive through interrupt + resume with stub models; SSE
   event shape; topology endpoint.
9. (Option B only) consented-secret host extension + image extra.
10. Dogfood on 3066; verify catalog, a live ops-triage run with the graph view
    lighting up, a fix-proposer pause/resume, and a LangSmith trace URL.

## Out of scope for v1

- PydanticAI adapter (contract is defined; adapter is a later build).
- The private-infra agents (content/publish/remediation/answer-engine).
- Cross-process / persistent HITL checkpointer (in-worker MemorySaver is v1).
- Per-host egress allowlist enforcement (deferred at the platform level already).

---

# Authoring + management direction (decided 2026-06-28)

This supersedes the earlier "build agents inside the Agent Fleet view" idea. The
corrected product framing: **AgeniusDesk is a control plane for code-authored AI
automations the same way it already is for n8n.** The agents are arbitrary
LangGraph + PydanticAI automations (any purpose, any tools), not n8n-ops helpers;
the three shipped agents are just bundled examples. Two surfaces, mirroring n8n:

- **Code Lab = build** (the n8n-editor analog). You author the agent here.
- **Agent Fleet = run + monitor** (the n8n-executions/errors analog). No in-app
  builder; Agent Fleet is monitor-only.

## Decisions

1. **Code Lab gets an Agent track.** A new mode alongside Code Node / Workflow
   Builder, with a **framework toggle: LangGraph | PydanticAI** (a toggle inside
   one Agent mode, not two modes). The template dropdown becomes agent starters
   (ReAct, HITL, parallel fan-out, pipeline, blank) per framework; the AI sidebar
   loads the agent skills (`langgraph-agent`, `langsmith-evals`) the way it loads
   n8n skills + n8n-mcp today; the existing Prompt Builder is reused for the system
   prompt; add an **Open in Studio** link (LangGraph). `Send to n8n` is joined by
   **`Register to Agent Fleet`**.

2. **Agents live in the VAULT.** Add an **`agents/` section to the vault** (Harness
   files the operator owns). `Register to Agent Fleet` writes the agent there
   (code + a small manifest: name, framework, model, trigger, enabled); the fleet
   **discovers** agents from `vault/agents/`. PydanticAI is a **peer to LangGraph,
   v1** (the adapter abstraction is not deferred).

3. **Agent Fleet is monitor-only.** Catalog of built agents with a **framework
   badge (LangGraph/PydanticAI)**, status, last run, **enable/disable**, and a
   **trigger** (manual/webhook/schedule). Runs, errors, and a health roll-up,
   mirroring the n8n error tracking + Fleet Health.

4. **Run view normalized to a WATERFALL for both frameworks**, so monitoring looks
   the same regardless of framework. This reuses the OTel observability waterfall
   already used for n8n executions, unifying observability. **LangGraph
   additionally** shows the live node-graph panel (for now); **PydanticAI** gets
   the waterfall only. The earlier asymmetry (graph+timeline vs timeline) is out.

## Build slices (the spine first)

1. **Vault `agents/` section + manifest + fleet discovery.** The runner loads
   agents from `vault/agents/` (plus the seeded examples). The connective tissue.
2. **`Register to Agent Fleet` from Code Lab** → writes the agent into
   `vault/agents/<name>/`. Closes the build->manage loop.
3. **PydanticAI adapter + a normalized run-event/span model** (spans with timing)
   that both adapters emit, feeding the waterfall.
4. **Waterfall run view** in Agent Fleet (both frameworks); LangGraph keeps the
   graph panel.
5. **Code Lab Agent mode UX** (framework toggle, agent templates, agent skills in
   the sidebar, Open in Studio).

Dependency: `pydantic-ai` joins the `langgraph`/`agent-fleet` extra (pin a
clean-venv resolution against the existing langchain/anthropic pins).

---

# Distribution: core, not community (decided 2026-06-28)

The roadmap locked all three integration modules as community (opt-in install).
That holds for Proxmox + the secret backends (self-contained, hold their own
creds). It does NOT survive the agreed Agent Fleet design, so **Agent Fleet ships
as a core built-in module** instead.

Why core:
- The design weaves agents into **Code Lab** (build), the **vault** (store), and
  the **OTel waterfall** (monitor) — all core. A community module is meant to call
  core, never the reverse; if the build logic lives in Code Lab, core would depend
  on a community module, which inverts the model.
- Going core also removes the community-iframe constraint that forced the run view
  to **poll** (the sandboxed iframe's `AgeniusDesk.fetch` is buffered). A core
  same-origin view streams over the dashboard WebSocket and reuses the waterfall
  component directly. The run view is genuinely live again.

How leanness is preserved (same shape as the `assistant` module):
- The module **code** ships in core (`backend/modules/agent_fleet/`); the heavy
  LangGraph/PydanticAI **deps** stay opt-in behind the `langgraph` extra
  (`AGD_EXTRAS=...,langgraph`). The default image stays lean.
- **Graceful degrade** when the extra is absent: the module imports at boot
  (lazy heavy imports), a run returns "LangGraph dependencies not installed", and
  Code Lab's Agent mode shows an "install the agent extra" nudge.

What "community" becomes for agents:
- **Agents are shareable as vault files** (export/import `vault/agents/<name>/`),
  no module install needed.
- **Community modules EXTEND the platform** (extra framework adapters, tool packs,
  prebuilt agent bundles) on top of the core runtime, not being it.

## Vault agent format (slice 1 — done 2026-06-28)

Operator-authored agents live in the vault at `data/workspace/agents/<id>/`:

- `agent.json` — manifest (metadata only): `name`, `framework` (`langgraph` |
  `pydantic-ai`), `model` / `model_env` / `max_tokens`, `tools` (names from the
  tool registry), `hitl`, `badges`, `tagline`, `description`, `run_hint`,
  `uses_errors`, `enabled`. `id` defaults to the folder name.
- `graph.py` — a PURE factory exporting `build(llm, tools, checkpointer=None)`, and
  OPTIONALLY `initial_state(task, target)` + `kickoff(error_id, prompt)`. It imports
  only langgraph/langchain; the host injects the model + the resolved tools, so the
  agent code never reaches AgeniusDesk internals (portable + safe).

`vault_agents.discover()` scans the folder, reads only the manifests (cheap,
boot-safe, no langgraph needed), and registers an `AgentDef` per agent whose
callables lazily import `graph.py` at run time (module cached by path+mtime, so a
Code Lab edit reloads automatically). `registry.all_agents()` merges built-ins +
vault agents fresh on each call, so a newly written agent appears with no restart.
A vault agent shows in the catalog even without the langgraph extra; running it then
surfaces the missing-dep error. Tool names resolve via `tools_local.resolve_tools`
(`tool_catalog()` lists them for the future Code Lab builder). Verified live: a
sample `quick-triage` vault agent was discovered and ran to completion.

This is the format Code Lab's "Register to Agent Fleet" (slice 2) writes and its
Agent templates (slice 5) scaffold.

Migration (done 2026-06-28): the community `agent-fleet` module became
`backend/modules/agent_fleet/` (builtin manifest, eager router, core auth, the
runner re-broadcasts `langgraph:run` over the WebSocket); the frontend became a
core view (`frontend/js/views/agent-fleet.js` + `agent-fleet-graph.js`, wired in
`app.js` + the index.html nav); `langgraph.json` + `studio.py` use dotted module
paths. The community copy is removed.
