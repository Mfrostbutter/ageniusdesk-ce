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

**Distribution (UPDATED 2026-06-28):** Agent Fleet now ships as a **CORE built-in
module**, not community. It weaves into Code Lab (build), the vault (store), and the
OTel waterfall (monitor), so it cannot be a community module without inverting the
module model; its LangGraph/PydanticAI deps stay opt-in via the `langgraph` extra
with graceful degrade. See the Agent Fleet spec's "Distribution: core, not
community". **Proxmox + the secret backends remain COMMUNITY** (opt-in, installed
via the GitHub inspect / scan / consent pipeline); see "Isolation & credentials"
for the credential-handling those two still force.

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
- Ships as a **community module** (opt-in). It holds cluster credentials, so it
  is a credential-holding module — see "Isolation & credentials" for how those
  creds reach it without breaking the sandbox.
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
- Awkwardest fit for "community module": a secret BACKEND inverts the bridge —
  the host wants to pull secrets FROM the module, whereas the bridge is built for
  the module to call the host. Likely needs a small host-side resolver the
  module registers, rather than a normal sandboxed worker. Flag for design.

**Open questions:**
- Resolution precedence (env > broker > local store?) and cache TTL.
- Per-instance / per-module secret scoping.
- agent-vault hybrid model specifics.

---

## Isolation & credentials (the thing "all community" forces us to design)

Distribution is locked to **community** for all three. That is clean for the
Agent Fleet — its only host secret is the LLM key, already handled host-side by
`assistant.complete`. It is NOT free for the credential-holding ones (Proxmox,
secret backends), because the bridge model is deliberately "the key never enters
the worker." Under the container/subprocess tiers the worker env is scrubbed, so
a sandboxed Proxmox module cannot just be handed the cluster token.

Three ways to reconcile (pick during build, not now):

1. **Extend the bridge (recommended, keeps the philosophy):** add host-mediated
   capabilities so the credential stays host-side — e.g. a generic
   `http.request` bridge method scoped to the module's declared `network.hosts`
   with the host injecting the consented auth, or a per-integration bridge
   namespace. The module orchestrates; the host makes the authenticated call.
   Same shape as `assistant.complete`.
2. **Consented-secret tier:** the operator explicitly accepts that this module
   receives specific secrets in-process (a trust step at install). Less pure;
   simplest to ship.
3. **Host-side resolver (secret backends specifically):** a secret backend is
   inverted from the worker model; it likely registers a thin host-side resolver
   rather than running purely as a sandboxed worker.

This is the central design question for the credential-holding modules. The
Agent Fleet does not block on it.

## Other cross-cutting decisions

- **CE vs Enterprise:** these are CE building blocks; multi-tenant governance
  (per-client RBAC, audit/receipts) stays on the separate enterprise roadmap.
- **First prototype order:** TBD (Agent runtime w/ LangGraph adapter, vs Proxmox
  as a simpler warm-up, vs PydanticAI adapter to prove the generalized contract).
- **Repo:** community modules live in `ageniusdesk-community-modules` alongside
  `youtube-research` (each its own folder + manifest).

## Not deciding yet

Release targets, exact manifests, and the `agent:run` event schema — those come
when we pick the first module to build.
