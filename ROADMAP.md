# Roadmap

AgeniusDesk Community Edition is a lightweight, open-source control plane for n8n. The roadmap prioritizes stability, extensibility, and the features operators need most.

Specs for in-progress and planned work live in [`docs/specs/`](docs/specs/).

## Current Release: v0.4.4 (2026-07-06)

The v0.4.2 to v0.4.4 line hardens and extends the platform on top of the v0.4.0 agent layer. See the [CHANGELOG](CHANGELOG.md) for full detail.

- **v0.4.4** adds **scheduled workflow backups** (per-instance snapshots to disk on an interval, fleet-wide, off by default) with an optional **offsite S3-compatible destination** (S3 / R2 / B2 / Wasabi / self-hosted MinIO, opt-in extra, encrypt-before-upload), plus local-model cost clarity in the waterfall.
- **v0.4.3** makes **Python Code nodes work out of the box** in deployed n8n by shipping the built-in template as a two-container bundle (external task runners plus a runners sidecar, Python standard library open by default).
- **v0.4.2** is a **security release**: four high-severity findings from the full security review plus the medium/low batch, with setup-wizard name/port fields and a configurable Overview error window.
- **v0.4.1** made the **n8n-only-by-default** agent gate a tagged release: a default install reads as a pure n8n control plane, and the Agent Fleet view + Code Lab's Agent Builder appear only when the optional agent extra is installed (or `AGD_AGENTS_ENABLED=true`).

**In development (see CHANGELOG [Unreleased]):** silent-failure detection (green but broken runs), tracked in Near-Term below.

## Previous Release: v0.4.0 (2026-06-28)

v0.4 keeps AgeniusDesk **n8n-first** and adds an **optional** agent layer on top (off by default): build real LangGraph and PydanticAI agents and run + monitor them the way you run workflows, plus batteries-included n8n intelligence. Highlights:

- **Agent Fleet (core built-in)**: a managed fleet of LangGraph + PydanticAI agents — catalog, run with a live graph and a normalized run waterfall, human-in-the-loop approve/resume, optional LangSmith tracing, and per-run token/cost. The agent stack is an opt-in dependency extra (`AGD_EXTRAS="assistant,langgraph"`) and **off by default**: a default install is n8n-only; the Agent Fleet + Agent Builder appear when the extra is installed (or `AGD_AGENTS_ENABLED=true`).
- **Agent Builder in Code Lab**: a third mode that builds agents (framework toggle, ReAct / human-in-the-loop / parallel-fan-out starters) and Registers them to the fleet. Agents live in your vault as files you own, edit, or delete.
- **Built-in n8n-mcp, auto-installed**: real n8n node knowledge, search, and workflow validation in Code Lab and the assistant out of the box (docs-only by default; one-click wire to the active instance).
- **n8n skill library in the Harness**: a curated `skills/` library seeded into the vault that the assistant loads on demand; the default Code Lab instructions route to it and the n8n-mcp tools.
- **Reliability fixes**: cross-port CSRF self-heal, strict-n8n workflow import, container port-collision warnings, the dashboard self-container guard, and pristine harness-seed refresh.

Full detail under "What shipped in v0.4.0" below; see the [CHANGELOG](CHANGELOG.md) for the complete entry.

## Earlier Release: v0.3.0 (2026-06-28)

v0.3 lands real isolation for community modules (the boundary the v0.2 scan/consent layer bridged), the agency multi-instance view, and a broader AI provider set, on top of the v0.2 core. Highlights:

- **Community-module isolation**: a sandboxed `iframe` for the frontend and opt-in out-of-process backend isolation (subprocess or hardened Docker container) behind a loopback capability bridge, so a module no longer runs in-process with host data and credentials
- **Fleet Health**: workflow health and errors rolled up across every connected n8n instance in one pane ("one client becomes ten")
- **Auto-install the error handler on connect**: a new instance starts reporting failures into the dashboard from the moment it is connected
- **More AI providers**: Perplexity, Groq, DeepSeek, Mistral, xAI (Grok), Together AI, and a Custom (OpenAI-compatible) base-URL provider

Full detail is under "What shipped in v0.3.0" below; see the [CHANGELOG](CHANGELOG.md) for the complete entry.

## Earlier Release: v0.2.0 (2026-06-27)

v0.2 lands full execution observability, the community-module install pipeline and its first module, and the authentication and onboarding layer, all on top of the v0.1 core. Highlights:

- **OpenTelemetry observability**: embedded OTLP receiver, the Observe trace waterfall, a metrics strip, and LLM cost enrichment folded into the trace layer
- **Community-module pipeline**: inspect / scan / consent install flow, monorepo discovery, a per-install audit trail, and one-click restart
- **YouTube Research module**: the first community module (captions to a structured breakdown, auto-filed into the notes vault)
- **Authentication and onboarding**: owner account, session login, optional TOTP, password reset, RBAC, CSRF, and per-view coachmarks

Full detail and checkboxes are under "What shipped in v0.2.0" below; see the [CHANGELOG](CHANGELOG.md) for the complete entry.

## Initial Release: v0.1.0 (2026-06-23)

### Completed Features

- Multi-instance n8n management with encrypted API key storage
- Real-time error feed and error grouping (by workflow, node, error type)
- Workflow management (list, activate/deactivate, trigger, import, export)
- Execution history with full-text search and filtering
- AI Assistant with OpenRouter, OpenAI, Anthropic, and local Ollama support
- MCP server integration for extending the assistant with external tools
- Code Lab with Monaco editor and AI code generation
- Knowledge management (sources, notes vault, full-text search)
- Encrypted secret store with `$VAR_NAME` references
- Docker container management with one-click deployment
- Community template library for common services
- Themes (Dark, Light, n8n) with custom theme support
- Music player (Spotify, YouTube, SoundCloud, Apple Music, Tidal)
- Insights dashboard (success rates, error trends, busiest workflows)
- Docker Compose deployment with setup wizard
- Comprehensive documentation and contributing guidelines

---

## What shipped in v0.4.0

The headline: AgeniusDesk operates AI agents the way it operates n8n.

### 1. Agent Fleet (core built-in) ([spec](docs/specs/2026-06-28-agent-fleet-langgraph-spec.md))

- [x] One managed-agents surface with **LangGraph** and **PydanticAI** adapters behind one run contract + catalog; built-ins ops-triage (ReAct tool loop), fix-proposer (human-in-the-loop), health-reporter (parallel fan-out).
- [x] Run + stream: the live LangGraph node graph **plus** a normalized run waterfall that renders the same for either framework; per-run token + cost. LangSmith tracing is optional (the OTel waterfall + price-book cost work without it).
- [x] Human-in-the-loop interrupt then approve/resume.
- [x] Agents live in your vault under `agents/<id>/` (a pure `graph.py` factory + an `agent.json` manifest); discovered live, no restart. **Delete** from the catalog (built-ins protected, blocked during a live run). Framework chip + "built-in" tag on cards.
- [x] Opt-in dependency extra (`AGD_EXTRAS="assistant,langgraph"`) keeps the default image lean.

### 2. Agent Builder in Code Lab

- [x] A third Code Lab mode: framework toggle (LangGraph | PydanticAI), per-framework starters, agent-aware AI assist, and **Register to Agent Fleet** writing the vault files. Build where you build n8n logic; monitor in the fleet.

### 3. Batteries-included n8n intelligence

- [x] **Built-in n8n-mcp** ([czlonkowski/n8n-mcp](https://github.com/czlonkowski/n8n-mcp), MIT), auto-installed in its own container when Docker is available (docs-only by default; one-click wire-to-instance for create/update/manage). Opt out with `AGD_N8N_MCP_AUTO=false`.
- [x] **n8n skill library** vendored from [czlonkowski/n8n-skills](https://github.com/czlonkowski/n8n-skills) (MIT) and seeded into the Harness; the default Code Lab instructions route to it and the n8n-mcp tools so workflows are built correctly the first time.

### 4. Reliability

- [x] Cross-port **CSRF self-heal**: two dashboards on `localhost` no longer 403 every mutation.
- [x] **Workflow import** survives n8n's strict create schema (top-level + nested `settings` allowlist).
- [x] **Container port-collision** pre-check plus a friendly bind-error message.
- [x] **Self-container guard**: the dashboard can no longer destroy or stop its own container.
- [x] **Harness seed refresh**: README / AGENTS refreshed on existing installs only while still pristine.

## What shipped in v0.2.0

Sequenced: observability first, then the community-module pipeline and its first module, on top of the authentication and onboarding layer.

### 1. OpenTelemetry observability ([spec](docs/specs/2026-06-26-opentelemetry-observability.md))

Push-based, per-node execution visibility. Hybrid design: an embedded OTLP/HTTP receiver MVP (spans/metrics to SQLite with bounded retention and a trace-waterfall Observability view) plus an optional one-click external stack (OpenTelemetry Collector + Tempo + Prometheus + Grafana). Additive to Insights, not a replacement.

- [x] OTLP/HTTP receiver (traces) with token auth (`AGD_OTEL_TOKEN`) and body limits
- [x] Span storage with bounded retention (age + row cap), pruned on ingest
- [x] Observe view: recent-traces list + parent/child waterfall, live-updating, plus a per-execution trace popup in workflow detail
- [x] Metrics strip (executions / error-rate / p50 / p95 / throughput), span-derived (n8n exports traces, not OTLP metrics)
- [x] Cross-links: per-execution Trace button in Errors; per-workflow "traces" deep-link from Insights into Observe
- [ ] Optional external-stack one-click template + Grafana linking (deferred)
- [x] **Cost observability** ([spec](docs/specs/2026-06-27-cost-observability.md)): LLM spend folded into the trace layer. n8n's spans carry no token/cost data, so cost is enriched from n8n run-data (per-call token usage) x a layered price book (OpenRouter-fetched > bundled, est-flagged), stored per span, surfaced as a Spend card, per-trace cost, and per-AI-span cost in the waterfall. Verified live (a Sonnet agent run priced at ~$0.34). Subsumes the old "Cost tracking integration" item. Now correct across a multi-instance fleet: traces are attributed to their source instance ([instance-attribution](docs/architecture/instance-attribution.md)) and run-data is fetched from that instance, so a non-active instance's spend is no longer silently `$0`. Follow-ups: operator price overrides UI, the cost-aware gateway for exact cache-aware cost.

### 2. Community module security: scan + consent ([spec](docs/specs/2026-06-26-community-module-security-and-youtube-research.md))

Make installing a community module a deliberate, informed act. Capability manifest, an AST static scanner, a two-phase inspect/install flow with proportional consent, and a tamper-evident audit trail. Heuristic review, not a sandbox; out-of-process (backend) and iframe (frontend) isolation are the deferred real boundaries (see Future Directions).

- [x] Capability manifest schema + validation
- [x] AST static scanner + fixtures (declared-vs-detected diff)
- [x] Two-phase inspect/install + consent + `module_installs` audit table
- [x] Consent modal + per-module capability/scan surfacing
- [x] Monorepo support: `discover` endpoint + traversal-safe `path` (one repo, many modules)
- [x] One-click restart to activate an installed or removed module
- [ ] Optional manifest signature verification (field shape reserved + provenance display shipped; verification deferred to v0.3)

### 3. YouTube research module (first community module)

Built against the pipeline above as its first consumer. Captions-only v1, Inbox -> classify + tag -> auto-file into the Harness research vault, with a scaffolded starter taxonomy. Distributed as its own GitHub repo and installed through the scan/consent flow. Whisper transcription fallback and isolation are deferred (see Future Directions).

### 4. Authentication and onboarding

- [x] Authentication and accounts: owner account, session login, optional TOTP two-factor, password reset, login throttling/lockout, and CSRF protection ([spec](docs/specs/2026-06-24-authorization-and-accounts.md))
- [x] Role-based access control: viewer / operator / admin enforced per router group
- [x] Onboarding: derived-state Setup Journey ("Get started" card) plus per-view page coachmarks ([spec](docs/specs/2026-06-24-onboarding-and-coachmarks.md))
- [x] Security hardening: central internal-API auth gate, opt-in edge-auth, webhook and MCP tokens, traversal guards, and the first automated test suite
- [x] AgeniusDesk wordmark on the login splash

### Release hygiene

- [x] Logout control in the app chrome (sidebar account row; finishes the auth spec, Section 7.3)
- [x] Persistent Code Lab across instance switch: the editor buffer survives re-render, so authoring on one instance and deploying to another no longer loses work
- [x] "Open" button per instance in the sidebar switcher: open an n8n instance's UI directly in a new tab

---

## Near-Term (Next 2-3 Months)

- [ ] **More container templates**: MySQL and more services (PostgreSQL, MongoDB, Redis, MinIO, Qdrant, Ollama, Flowise already ship as built-in templates)
- [ ] **Richer Code Lab**: a curated code-snippets library and an in-app n8n node-documentation sidebar (template expansion and `$`-autocomplete already ship; deep node knowledge is available now via the built-in n8n-mcp)
- [ ] **Additional knowledge connectors**: HTTP fetch, GitHub, API connectors beyond Qdrant
- [x] **Harness skills section**: a library of skills in the Harness (`skills/`) that agent instructions point at, so an agent loads focused, domain-specific guidance on demand. Seeded into the vault on first run; router note at `skills/README.md` (shipped — see CHANGELOG [Unreleased])
- [x] **Curate high-quality n8n skills**: the full czlonkowski/n8n-skills set (MIT) — workflow patterns, node config, expressions, Code nodes, error handling, validation, agents, and more — vendored as the starting content for the Harness skills section
- [ ] **Workflow version history**: snapshot on import, diff viewer, restore from snapshot
- [x] **Scheduled backups**: automated per-instance backup with configurable retention. A dependency-free internal interval scheduler snapshots every connected instance's workflows to `data/backups/<instance>/` on a schedule (enable / interval / retention / active-only on the Export / Backup view; `/api/backups` endpoints), fanning out across the fleet and isolating a failing instance. Off by default. Shipped — see CHANGELOG [Unreleased]. The scheduler is the shared prerequisite the scheduled-health-report item below now builds on.
  - [x] **Offsite backup destination (S3-compatible)**: push each snapshot to S3 / R2 / B2 / Wasabi / self-hosted MinIO behind an opt-in `s3` extra, with a test-connection probe, optional offsite retention mirroring, and optional Fernet encryption before upload. Credentials via secret-store refs only. Push-only in v1. [Spec](docs/specs/2026-07-06-offsite-backup-s3-sink.md). Deferred: Google Drive / OAuth destinations and an rclone shell-out (broader backend coverage), plus restore-from-remote UI.
- [ ] **Scheduled health reports**: an automated, recurring (e.g. monthly) per-instance workflow health report, generated and delivered without anyone opening the dashboard. Rolls the period's success/error rates, error trends, busiest and slowest workflows, and notable incidents (from Insights + Fleet Health) into a client-ready summary, delivered over the notification sinks or email. Builds on the on-demand health-reporter agent (its parallel fan-out becomes a scheduled job) and feeds the agency client-reporting loop.
- [ ] **Health monitoring**: surface uptime via an **Uptime Kuma connector** (read the operator's existing monitors over Kuma's API and fold up/down + uptime % into Fleet Health) rather than rebuilding generic endpoint polling. Native HTTP/TCP checks remain a later fallback for operators not already on Kuma. See [community-module candidates](docs/specs/2026-06-28-community-module-candidates.md).
- [x] **Local-model cost clarity** ([spec](docs/specs/2026-07-02-local-model-cost-clarity.md)): the price book already tracks token usage for every provider (n8n run-data is provider-agnostic), but Ollama and self-hosted Custom-endpoint models fell through to `price_source: "unknown"` since they're absent from OpenRouter and the bundled table. Ollama node types are now tagged `local` (via `n8n.node.type`) so the waterfall surfaces token usage with a plain "local" tag instead of the ambiguous "price unknown" or a meaningless dollar figure. Custom-endpoint base-URL sniffing is deferred (see spec Non-goals). Shipped — see CHANGELOG [Unreleased].
- [ ] **Expanded notification sinks**: email, PagerDuty, webhook routing per instance
- [x] **Silent-failure detection (green but broken runs)**: catch runs n8n marks success while a node errored under Continue-On-Fail or quietly stopped producing data, the failure class with no failed execution to alert on. On OpenTelemetry ingest it reads output shape rather than status (a normalized demoted-error union plus per-node output-volume-vs-history), suppresses drop cascades to the origin node so one root cause is one alert, and surfaces a distinct `Silent failure` class across the Overview card, Insights, the Observe metrics strip, and the Errors feed. Tunable per instance via `AGD_HEALTH_*`. Shipped (see CHANGELOG [Unreleased]). [Architecture note](docs/architecture/silent-failure-detection.md), [spec](docs/specs/2026-07-07-silent-failure-detection.md). Follow-ups:
  - [ ] **Dead-man's-switch (never-ran node)**: a node that produced no span at all (branch skipped, instance down, schedule missed) is not yet detected. Two layers: an external heartbeat for "the workflow never fired," plus a definition-vs-trace node diff for "a node went missing inside a run that did fire."
  - [ ] **Configurable expected-output thresholds**: a per-node declared output floor/range so "returned 10, always returns 100" fires explicitly rather than only via the learned drop heuristic. Config on the node, policy defaults roll down from the workspace, and values are suggested from history (one-click accept, only prompting the steady producers that matter) so per-node config scales. Doubles as the per-node override for cases history infers wrong.
  - [ ] **Upstream n8n OTel error semantics** (feature request): get the continued error onto the OpenTelemetry span (standard exception attributes plus span status) so any backend can read it, since n8n currently holds the typed error and then exports the Continue-On-Fail span as OK. Would make detection easier for the whole ecosystem, not just AgeniusDesk. AGD's consumer side is ready: detection prefers the typed `taskData.continuation` rollup a patched n8n records and gates the unsound content-scan behind `AGD_HEALTH_SCAN_LOOSE_JSON_ERROR`, so a patched instance drops the loose-`json.error` false positives. The upstream PR (engine-level continued-error signal) is in review.
- [ ] **Workflow security audit scan**: detect missing error handlers, unused credentials, exposed webhooks (this audits n8n workflows; distinct from the community-module code scanner in v0.2)
- [ ] **Project landing page**: a public web page introducing AgeniusDesk CE (overview, screenshots, install, docs and repo links)

---

## What shipped in v0.3.0

The headline is real isolation for community modules, the boundary the v0.2 scan/consent layer bridged, plus the agency multi-instance view and a broader provider set.

### 1. Community-module isolation ([spec](docs/specs/2026-06-27-out-of-process-backend-isolation.md))

- [x] **Frontend iframe isolation**: render each community view in a sandboxed `iframe` (`allow-scripts`, no `allow-same-origin`) with a postMessage RPC bridge to a whitelisted host API (`fetch` / `notify` / `navigate` / `openInHarness`), plus theme propagation and auto-resize. A module's frontend can no longer read, change, or break the host UI; it reaches the host only over the bridge, and `fetch` is restricted to same-origin `/api/` paths.
- [x] **Out-of-process backend isolation**: run a module's Python outside the app process behind the capability bridge, so a module no longer runs in-process with full data and credential access. Two operator-selectable tiers (Settings > Modules / `AGD_MODULE_ISOLATION`): **subprocess** (sandboxed child process, blocked host imports, scrubbed env) and **container** (own hardened Docker container: read-only rootfs, dropped capabilities, no socket, resource limits, isolated network). Privileged actions go through the bridge (vault scoped to declared paths; tool-free `assistant.complete` with the key host-side). The reference YouTube Research module is dual-mode (same code in-process or isolated).
- [x] Two adversarial pre-release reviews ([host-bridge](docs/specs/2026-06-27-host-bridge-review.md), [pre-release](docs/specs/2026-06-27-isolation-prerelease-review.md)) plus a focused [re-check](docs/specs/2026-06-28-isolation-prerelease-recheck.md), all closed.
- [ ] Remaining hardening (non-root container uid, per-host egress enforcement) tracked for v0.3+.

### 2. Fleet Health and error-handler auto-install

- [x] **Fleet Health view**: workflow health and errors rolled up across every connected instance (per-instance active/total workflows, error rate, unhealthy workflows, plus a combined total). Live parallel fan-out; a degraded instance is shown, not fatal.
- [x] **Auto-install the error handler on connect**: adding an instance best-effort installs + activates the Global Error Handler into it (idempotent), with a container-reachable dashboard URL. The handler carries the `AGD_WEBHOOK_TOKEN` header so delivery survives a token-gated dashboard.
- [x] **Shared error item**: one error renderer used identically on Overview, Errors, and Fleet Health (Ask AI, Trace, View Workflow, Open in n8n, delete/clear).

### 3. Broader AI provider set

- [x] Native support for **Perplexity, Groq, DeepSeek, Mistral, xAI (Grok), Together AI**, plus a **Custom (OpenAI-compatible)** base-URL provider, alongside the existing OpenRouter / OpenAI / Anthropic / Ollama.

### 4. Pre-release security hardening

- [x] Modules-management and error-mutating endpoints gated at the operator role; `notes.search` scoped by resolved (symlink-safe) path; container/volume teardown made mode-independent.
- [x] Fixed a stored-XSS class in the shared error item (attribute/JS-context escaping of attacker-influenced ids from the error webhook); added a behavioral regression test.

## Community Modules & Homelab Pack (Concept)

AgeniusDesk's chrome (Fleet Health, Errors, Ask AI, Observe, Notes) is the reuse
surface: the highest-value community modules fold into it rather than standing alone,
moving CE from "an n8n control plane" toward "the homelab / automation control
plane." Full landscape, per-candidate buildability verdicts, and the pack contents:
[community-module candidates](docs/specs/2026-06-28-community-module-candidates.md).

Two **host investments** gate the whole quadrant (do these before the modules):

- [ ] **`http.request` bridge** (highest leverage): host-mediated outbound HTTP with the credential injected host-side, so a credential-holding module is safe under isolation instead of `in_process`-only. Unlocks the entire REST quadrant — Cloudflare, NocoDB/Baserow/Airtable, Qdrant, object storage, Uptime Kuma, **Proxmox**, **Home Assistant**, reverse proxy, Pi-hole/AdGuard, Tailscale/NetBird, TrueNAS — at once. [Spec](docs/specs/2026-06-28-http-request-bridge.md).
- [ ] **Fleet Health contribution API**: let a loaded module publish `{label, status, metrics}` rows that `fleet_health()` merges, so module health (cluster nodes, queue workers, NAS disks, tunnel status) renders in the Fleet Health pane.

**Homelab Pack v1 core**: Proxmox, Remote Docker/Portainer, NAS health (TrueNAS), Uptime Kuma, Cloudflare, Home Assistant. **Extended**: reverse proxy, Pi-hole/AdGuard, Tailscale/NetBird, Authentik. Distributed via the existing bundle mechanism.

The **Redis/queue monitor** and a **database viewer** are wanted but hit the native-wire-protocol wall (no driver delivered by the installer); the DB viewer is better as a built-in. Tracked in the candidates doc.

Not every valuable module folds into the existing chrome; some **add their own surface**:

- [ ] **Support / ticketing module** (opt-in community module, its own UI): a dedicated ticketing view where inbound client support email lands. Agencies running AgeniusDesk field support over a shared address (`support@...`); route that mailbox into the module (IMAP poll or forward-to-webhook) so each thread becomes a ticket in its own inbox, with a status lifecycle (open to resolved), AI triage and draft replies through the assistant, and optional cross-links to the workflow or execution a request concerns. Deliberately **separate from Errors**: error reporting is machine-generated workflow failures, support tickets are human requests; the ticket UI can reference an error but does not live in it. Distributed through the community-module pipeline; pairs with multi-tenancy for per-client routing and feeds the client-reporting loop.

---

## Medium-Term (v0.3+ Concept)

- [ ] **Multi-tenancy foundation**: group instances and workflows by client or team
- [ ] **Audit logging**: track all user actions for compliance (extends the per-install module audit from v0.2)
- [ ] **Cost tracking** — folded into Observability ([cost-observability spec](docs/specs/2026-06-27-cost-observability.md)); LLM spend is the cost dimension of the trace store, not a standalone feature
- [ ] **Workflow promotion**: promote workflows across dev, staging, production instances
- [ ] **Public API hardening**: expand and stabilize the existing versioned `/api/v1` (X-API-Key) surface

---

## Future Directions

- Module isolation (frontend iframe + out-of-process backend) is the real security boundary; shipped in v0.3.0 (see "What shipped in v0.3.0" above). Remaining hardening: non-root container uid, per-host egress enforcement
- Whisper transcription fallback for the YouTube research module (videos without captions; never a bundled GPU dependency)
- Workflow diff viewer (visual side-by-side comparison)
- Secret backends as core built-ins — Infisical (boot-time env hydration + dashboard CRUD) and Agent Vault (mirror-in, audited egress broker), ported from the beta with a phased Docker-sandbox path to real key isolation; spec: `docs/specs/2026-07-03-secret-backend-ce-port.md`. Earlier community-module framing is superseded.
- Other external secret sources (1Password, AWS Secrets Manager, HashiCorp Vault)
- Git integration (export workflows to repos, branch-based environments)
- SAML/LDAP for team authentication
- Agentic workflow management — **shipped** as the Agent Fleet core built-in (LangGraph + PydanticAI adapters, live graph view, LangSmith tracing); `backend/modules/agent_fleet/`
- Client-facing portal (scoped workflow access for non-operators)
- Home Assistant integration — now part of the Homelab Pack (see "Community Modules & Homelab Pack" above)
- Support for other automation platforms (Make, Zapier)

---

## How to Contribute

We welcome pull requests for:
- Bug fixes and stability improvements
- New container templates
- Additional knowledge connectors
- UI/UX enhancements
- Documentation improvements
- Test coverage (pytest)

See [CONTRIBUTING.md](CONTRIBUTING.md) for dev setup and guidelines.

---

## Feedback

Found a bug or have a feature request? Please open a [GitHub issue](https://github.com/Mfrostbutter/ageniusdesk-ce/issues).
