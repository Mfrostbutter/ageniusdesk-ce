# Roadmap: Next Integration Modules

Status: ROADMAP / scoping only. Nothing here is committed to a release yet — this
captures direction + locked decisions + open questions so the build is fast when
we start.

Date: 2026-06-28

## Frame: build on what we have

New modules ride the existing module system, not a new stack:

- `manifest.json` (id, capabilities, `routes_prefix`, `frontend.nav`) + a FastAPI
  `router` + a frontend view — same shape as the `youtube-research` reference.
- The **capability bridge** (loopback, per-spawn token): scoped vault access +
  tool-free `assistant.complete` (LLM key stays host-side).
- The **three isolation tiers** (in_process / subprocess / container).
- The **self-protection** pattern just shipped for Docker (never act destructively
  on the resource the dashboard itself depends on) generalizes to other infra.

Two families: an **agent runtime** (LangGraph + PydanticAI), an **infra
integration** (Proxmox), and **secret-backend integrations** (Infisical /
agent-vault).

---

## 1. Agent Fleet — one runtime, framework adapters

**Decision (locked):** a single managed-agents surface with **LangGraph** and
**PydanticAI** as pluggable framework **adapters** behind one `agent:run`
contract + catalog — NOT two separate modules. Generalizes the LangGraph agent
contract already running in the beta ("personal" AgeniusDesk).

**Surface (shared across adapters):**
- Agent **catalog** (register an agent; adding one is one definition).
- **Run + stream**: kick off a run, stream steps/tokens/state to the UI.
- **Human-in-the-loop**: interrupt → approve/resume.

**Hard requirements (must retain from the beta setup):**
- **LangSmith tracing.** Runs are traceable in LangSmith exactly as today — keep
  the tracing wiring; operator supplies their LangSmith key/project.
- **Live LangGraph graph view** as runs execute — the node-by-node live view we
  have in the personal version. Framework-agnostic where possible, but the rich
  graph view is LangGraph-specific first; PydanticAI gets at least run/step
  streaming.

**Fit to the architecture:**
- Runs on the **container isolation tier** — these execute third-party agent /
  graph code; that's exactly what the tier was built for.
- Reaches the host only through the **bridge**: `assistant.complete` for the LLM
  (key never enters the worker) and notes for memory.
- One `agent:run` event contract; LangGraph + PydanticAI adapters emit it.

**Open questions:**
- Built-in vs community vs mixed (see cross-cutting below).
- How LangSmith config/keys flow (per-operator secret; per-agent project?).
- Is the live graph view LangGraph-only at first, with PydanticAI on plain
  run/step streaming until it has a graph to render?
- Where agents are authored/stored (in-repo definitions vs installable agents on
  top of the runtime).

---

## 2. Proxmox — plug-and-play infra module

**Intent:** an operator authorizes it and points it at the **management console
URL**; that's it. Same low-friction onboarding as adding an n8n instance.

**Scope:**
- Connect: console URL + auth (API token preferred; user/realm fallback), test
  on save.
- List / inspect / start / stop / reboot VMs and LXCs; node + cluster health.
- Fold the roll-up into **Fleet Health** ("one client becomes ten" → also "one
  cluster, N nodes").

**Fit / safety:**
- Trusted **built-in** (holds cluster credentials), like `n8n_proxy` /
  `docker_mgr`.
- Inherits the **self-protection** rule: never let the app destroy/stop the
  Proxmox node or VM/LXC the dashboard itself runs on — that's a console action.
- Read-first; destructive actions gated at the operator role + confirmation.

**Open questions:**
- API token vs user/password/realm as the default auth.
- Read-only mode toggle for the cautious.
- CE building block vs enterprise (governance) — see below.

---

## 3. Secret-backend integrations — Infisical / agent-vault

> Parse check: "physical and agent vault" read as **Infisical + agent-vault**
> (an Infisical-compatible credential broker). Correct if mis-heard.

**Intent:** let AgeniusDesk resolve secrets from an external broker, not only the
local store. Today resolution is env var → encrypted `secrets.json`; add Infisical
and agent-vault as **resolution backends**.

**Scope:**
- Connect: broker URL + machine identity / token; operator authorizes.
- Resolve `$NAME` references against the broker (with the existing precedence and
  caching); secrets never get written to disk in plaintext.
- Plug-and-play onboarding, same as Proxmox.

**Fit:**
- Extends the existing secret-resolution layer rather than replacing it.
- Strong pairing with the Agent Fleet (agents get brokered, rotated credentials
  via the bridge, never raw keys).

**Open questions:**
- Resolution precedence (env > broker > local store?) and cache TTL.
- Per-instance / per-module secret scoping.
- agent-vault hybrid model specifics.

---

## Cross-cutting decisions to make

- **Distribution per module:** built-in (first-party, trusted — natural for
  Proxmox and the secret backends, which hold credentials) vs community
  (inspect/consent pipeline, runs under isolation — natural for third-party
  agents). Likely **mixed**: Proxmox + secret backends + the Agent runtime as
  built-in; individual agents as installable content on top of the runtime.
- **CE vs Enterprise:** these are CE building blocks; multi-tenant governance
  (per-client RBAC, audit/receipts) stays on the separate enterprise roadmap.
- **First prototype order:** TBD (Agent runtime w/ LangGraph adapter, vs Proxmox
  as a simpler warm-up, vs PydanticAI adapter to prove the generalized contract).

## Not deciding yet

Release targets, exact manifests, and the `agent:run` event schema — those come
when we pick the first module to build.
