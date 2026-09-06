# Roadmap

AgeniusDesk Community Edition is a lightweight, open-source control plane for n8n. The roadmap prioritizes stability, extensibility, and the features operators need most.

Specs for in-progress and planned work live in [`docs/specs/`](docs/specs/).

## Current Release: v0.6.0 (2026-09-06)

v0.6 unlocks the credential-holding community-module quadrant: a module can now talk to an external service without ever holding the credential or opening its own connection, in any isolation tier. Highlights:

- **`http.request` host bridge**: a module declares operator-consented endpoints; the host owns the base URL, injects the credential per call, decides the TLS policy, and dials only a pinned address. The whole REST quadrant of the Homelab Pack becomes safe under isolation instead of `in_process`-only.
- **Trusted worker identity**: every community route is authorized by role (viewer reads, operator writes) and stamped with a spoof-proof actor; a browser cannot forge the `X-AGD-*` identity headers, and the module reads them for audit.
- **Read-only endpoint grants**: the operator can hold a module to `GET`/`HEAD` at the host bridge, so a read-only install is enforced below what a compromised worker could reach, not just hidden in the UI.
- **Proxmox ships as the first credential-holding community module**: nodes, VMs, and LXCs with live status and cluster health, plus gated power and provisioning, all through the bridge.
- **Trace backfill, the PSA ticket sink, and a public agent-run API**: rebuild lost traces from n8n's own execution history, turn error groups into PSA tickets, and start any fleet agent over the versioned public API with MCP servers exposed as agent tools — the agentic-MSP seam.
- **Instance rename and API-key rotation** on the Instances page, with a test-before-save probe.

Full detail under "What shipped in v0.6.0" below; see the [CHANGELOG](CHANGELOG.md) for the complete entry.

## Previous Release: v0.5.0 (2026-08-13)

v0.5 is about failures you cannot see and environments you could not previously move between. Highlights:

- **Silent-failure detection**: catch the runs n8n reports as success while a node errored under Continue-On-Fail or quietly stopped producing data. The failure class with no failed execution to alert on.
- **Dead-man's switch (layer 1)**: catch the node that never ran at all inside a run that did fire, by diffing the workflow's declared nodes against the spans that landed.
- **Workflow promotion**: move a workflow dev to staging to prod with a credential preflight, auto-provision from the Secrets store, and activation guarding. The open-source answer to n8n Enterprise environments.
- **The assistant asks before it acts**: a state-changing tool call now returns as an approval card rather than running inside the chat turn, so a prompt injection in an error payload, a RAG hit, or MCP output cannot drive it unattended.
- **Correct multi-instance observability**: traces are attributed to the instance that produced them, and cost and health enrichment fetch run-data from that instance, so a non-active instance's spend is no longer silently `$0`.

Full detail under "What shipped in v0.5.0" below; see the [CHANGELOG](CHANGELOG.md) for the complete entry.

## Earlier Release: v0.4.4 (2026-07-06)

The v0.4.2 to v0.4.4 line hardens and extends the platform on top of the v0.4.0 agent layer. See the [CHANGELOG](CHANGELOG.md) for full detail.

- **v0.4.4** adds **scheduled workflow backups** (per-instance snapshots to disk on an interval, fleet-wide, off by default) with an optional **offsite S3-compatible destination** (S3 / R2 / B2 / Wasabi / self-hosted MinIO, opt-in extra, encrypt-before-upload), plus local-model cost clarity in the waterfall.
- **v0.4.3** makes **Python Code nodes work out of the box** in deployed n8n by shipping the built-in template as a two-container bundle (external task runners plus a runners sidecar, Python standard library open by default).
- **v0.4.2** is a **security release**: four high-severity findings from the full security review plus the medium/low batch, with setup-wizard name/port fields and a configurable Overview error window.
- **v0.4.1** made the **n8n-only-by-default** agent gate a tagged release: a default install reads as a pure n8n control plane, and the Agent Fleet view + Code Lab's Agent Builder appear only when the optional agent extra is installed (or `AGD_AGENTS_ENABLED=true`).

## Earlier Release: v0.4.0 (2026-06-28)

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

## What shipped in v0.6.0

The headline: a community module can reach an external service without ever holding the credential, and the agentic-MSP integration seam.

### 1. `http.request` host bridge, trusted identity, read-only grants ([spec](docs/specs/2026-09-05-host-http-bridge-build.md))

- [x] A module declares `capabilities.host.http.endpoints` (id, suggested base URL, an auth shape that names a secret, methods, TLS policy). The operator confirms or overrides the base URL at install and chooses whether the module may change data; **mutating methods are off by default**. The host persists the effective config as an endpoint revision in `data/module-endpoints.json` and pins the resolved IPs — the manifest is never the runtime source of truth.
- [x] One request implementation for every isolation mode: isolated workers call `POST /api/_host/http/request`; in-process modules call the same code directly. Per call the host enforces the granted endpoint and method, validates the relative path, keeps the URL on the consented origin, drops any worker-supplied `Authorization`/`Host`/`Cookie`, resolves the secret at call time, dials only a pinned address (a host that resolves elsewhere fails closed pending re-pin), disables redirects, caps the body at 5 MB, and allowlists the echoed response headers. Unknown `host` manifest fields now fail validation instead of being ignored.
- [x] **Trusted worker identity**: one middleware over every `/api/{community-module}/...` request strips inbound `X-AGD-*`, authorizes by route class (viewer reads, operator writes, raised by the manifest's `routes` block but never lowered), and stamps trusted `X-AGD-User` / `X-AGD-User-Id` / `X-AGD-Role` / `X-AGD-Auth-Source` for the module to audit against.
- [x] **Read-only grants** enforced at the host: reducing an endpoint to `GET`/`HEAD` makes the bridge reject a mutating call even if the worker issues it directly. Settings > Modules gains an Upstream endpoints panel (status, base URL, granted methods, pinned IPs, configure, re-pin); the install consent modal shows each endpoint with a separate change-data acknowledgement.
- [x] Scanner reports declared bridge use and `verify_tls: false` endpoints as INFO, undeclared use as HIGH. The reverse proxy now closes the upstream stream on success, upstream failure, and client disconnect, with regression tests for XLSX and ZIP downloads.

### 2. Proxmox community module (first credential-holding module)

- [x] Proxmox 0.2.0 in [`ageniusdesk-community-modules`](https://github.com/Mfrostbutter/ageniusdesk-community-modules) consumes the bridge: nodes, VMs, and LXCs with live status and cluster health, plus gated start/stop/reboot and provisioning. It holds no credential and opens no direct connection in any tier; the token is injected host-side. Sets `min_app_version` to 0.6.0 and drops its unrestricted network declaration. Verified end to end against a live four-node cluster.

### 3. Agentic-MSP integration seam

- [x] **Trace backfill** ([spec](docs/specs/2026-08-14-trace-backfill-from-execution-history.md)): rebuild missing traces from n8n's own execution records (per-node timing, status, item counts) so a receiver outage or a token drift is recoverable instead of a permanent hole. Rebuilt traces render, price, and run silent-failure detection like real ones; ids are deterministic and real telemetry always outranks a reconstruction. On-demand over a range in v1 (`GET/POST /api/otel/backfill/*`, a **Rebuild traces** action on Observe).
- [x] **Ticket sink** (built-in, off by default): files one PSA ticket per error group through an itops-mcp tool plane, a throttled Internal reply on recurrence, and re-arms with a referencing ticket after closure. Group-to-ticket state is a new table; the hook into error ingest is fire-and-forget.
- [x] **Public run-start API + MCP servers as fleet tools**: start any registered fleet agent by id over the versioned public API (`X-API-Key`, trigger scope, shared single-flight with the dashboard), and let vault agents declare `mcp__{server}__{tool}` names that resolve to discovered MCP-server tools (bearer via Secrets refs). MCP servers can be registered with an explicit slug id so those names are stable across installs.
- [x] **HITL resume carries the reviewer's identity**: `POST /api/agent-fleet/runs/{id}/resume` threads a `by` field into the decision, so an attributed gate records who approved instead of a fallback.

### 4. Fleet and reliability

- [x] **Instance rename and API-key rotation** on the Instances page: rename touches nothing else; rotation tests the new key against the instance (honoring per-instance TLS) before saving, and the old key stays valid in n8n until revoked there.
- [x] The built-in **MCP server no longer disappears from fresh builds**: the `mcp` pin gained an upper bound and the image now builds from the committed lockfile, so the shipped artifact is the tested dependency set.
- [x] A **built-in module that fails to load says so** (ERROR with traceback, re-stated after the module roster), and **Observe distinguishes "no traces anywhere" from "none from this instance"** with a per-instance span badge and targeted setup instructions.

## What shipped in v0.5.0

The headline: the failures n8n's own status cannot report, and moving work between environments.

### 1. Silent-failure detection ([architecture](docs/architecture/silent-failure-detection.md), [spec](docs/specs/2026-07-07-silent-failure-detection.md))

- [x] Detect "green but broken" runs on OpenTelemetry ingest by reading **output shape rather than status**: a normalized union of the three places n8n records a demoted error, plus a per-node output-volume-versus-history classifier.
- [x] **Drop cascades suppressed to the origin**, so one root cause is one alert instead of fifteen.
- [x] Surfaced as its own `Silent failure` class everywhere at once: a dedicated Overview card, tiles on Insights and the Observe metrics strip, a `SILENT` badge with jump-to-trace in the Errors feed, and a distinct amber block on the Overview Execution Timeline.
- [x] Prefers the **sound** typed `taskData.continuation` signal where the instance runs a patched n8n; the unsound content-scan is gated behind `AGD_HEALTH_SCAN_LOOSE_JSON_ERROR` (on by default so stock n8n keeps full recall).
- [x] **Dead-man's switch, layer 1**: flag a declared node that had input available but produced no span at all, graph-aware and gated on run-history for precision.
- [ ] Dead-man's switch, layer 2 (the workflow never fired at all) needs an external heartbeat. Specced, not built: [spec](docs/specs/2026-07-11-heartbeat-dead-mans-switch-layer-2.md).

### 2. Workflow promotion ([guide](docs/guide/promote.md))

- [x] A **Promote** view and `n8n_promote` module moving workflows dev to staging to prod, the open-source answer to n8n Enterprise environments.
- [x] **Preflight** reports every credential a workflow binds, whether the target ships that type, and duplicate-name collisions, before anything is written. Changing the target or selection invalidates it.
- [x] **Credential auto-provision** reuses an already-mirrored target credential or creates one from the Secrets store, through the same SSRF, instance-scope, and URL-repoint guardrails as the manual mirror route. Ambiguity is surfaced, never guessed; provisioning is idempotent by reuse, not delete-and-recreate.
- [x] **Activation guarding**: a workflow whose mapped credential has no name on the target is refused rather than imported to fail at run time, and n8n's node-by-node rejection detail is surfaced.

### 3. The assistant asks before it acts

- [x] State-changing tool calls return as a **proposal on an approval card** (operator-gated, CSRF-checked, single-use, expiring) instead of running mid-turn. The gate lives in a shared `_dispatch_tool`, so it covers both the OpenAI-compatible and Anthropic tool loops, and the card renders on all six chat surfaces.
- [x] **MCP tools classified per server** (`writes` / `all` / `none`) from the server's own `readOnlyHint` annotations with a naming-convention fallback; an unclassifiable tool fails closed. Verified against a live n8n-mcp tool list, pinned as a test fixture.
- [x] `AGD_ASSISTANT_AUTORUN` restores unattended execution for a headless install.

### 4. Multi-instance observability correctness ([architecture](docs/architecture/instance-attribution.md))

- [x] Traces are **attributed to the instance that produced them**, via a deterministic `agd.instance.name` resource attribute on provisioned instances plus a one-time learn step for external or legacy ones. An unplaceable exporter parks in a stable bucket rather than landing on the active instance.
- [x] Cost and silent-failure enrichment **fetch run-data from the trace's owning instance**, so a non-active instance's spend and health are no longer silently empty.
- [x] Cost enrichment runs **eagerly on ingest**, so aggregate Spend counts every run rather than only traces someone opened.

### 5. Hardening

- [x] Public API keys gain optional **expiry, IP/instance/workflow scoping, and a per-key rate limit**, with per-request audit. An absent field means unrestricted, so existing keys are unaffected.
- [x] **Per-instance `tls_verify`** replaces the fleet-wide switch, so trusting one self-signed box no longer downgrades egress everywhere. Optional `AGD_EGRESS_ALLOW_CIDRS` narrows server-side fetches.
- [x] **Unauthenticated ingest bounded**: per-IP rate limits on the webhooks and OTLP, prune-before-insert so the span row cap is a real ceiling, span-attribute size bounds, and a startup warning when the webhooks are left open.
- [x] Defense-in-depth batch: central audit sink, promoted secrets no longer copied into `os.environ`, same-origin CORS default, constant-time MCP ping compare, deploy-time Docker `HostConfig` re-check, split password/TOTP lockout counters, reset-token rate limit, `__Host-` session cookie over HTTPS.

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
- [x] **Harness skills section**: a library of skills in the Harness (`skills/`) that agent instructions point at, so an agent loads focused, domain-specific guidance on demand. Seeded into the vault on first run; router note at `skills/README.md` (shipped — see CHANGELOG v0.4.0)
- [x] **Curate high-quality n8n skills**: the full czlonkowski/n8n-skills set (MIT) — workflow patterns, node config, expressions, Code nodes, error handling, validation, agents, and more — vendored as the starting content for the Harness skills section
- [ ] **Workflow version history**: snapshot on import, diff viewer, restore from snapshot
- [x] **Scheduled backups**: automated per-instance backup with configurable retention. A dependency-free internal interval scheduler snapshots every connected instance's workflows to `data/backups/<instance>/` on a schedule (enable / interval / retention / active-only on the Export / Backup view; `/api/backups` endpoints), fanning out across the fleet and isolating a failing instance. Off by default. Shipped — see CHANGELOG v0.4.4. The scheduler is the shared prerequisite the scheduled-health-report item below now builds on.
  - [x] **Offsite backup destination (S3-compatible)**: push each snapshot to S3 / R2 / B2 / Wasabi / self-hosted MinIO behind an opt-in `s3` extra, with a test-connection probe, optional offsite retention mirroring, and optional Fernet encryption before upload. Credentials via secret-store refs only. Push-only in v1. [Spec](docs/specs/2026-07-06-offsite-backup-s3-sink.md). Deferred: Google Drive / OAuth destinations and an rclone shell-out (broader backend coverage), plus restore-from-remote UI.
- [ ] **Scheduled health reports**: an automated, recurring (e.g. monthly) per-instance workflow health report, generated and delivered without anyone opening the dashboard. Rolls the period's success/error rates, error trends, busiest and slowest workflows, and notable incidents (from Insights + Fleet Health) into a client-ready summary, delivered over the notification sinks or email. Builds on the on-demand health-reporter agent (its parallel fan-out becomes a scheduled job) and feeds the agency client-reporting loop.
- [ ] **Health monitoring**: surface uptime via an **Uptime Kuma connector** (read the operator's existing monitors over Kuma's API and fold up/down + uptime % into Fleet Health) rather than rebuilding generic endpoint polling. Native HTTP/TCP checks remain a later fallback for operators not already on Kuma. See [community-module candidates](docs/specs/2026-06-28-community-module-candidates.md).
- [x] **Local-model cost clarity** ([spec](docs/specs/2026-07-02-local-model-cost-clarity.md)): the price book already tracks token usage for every provider (n8n run-data is provider-agnostic), but Ollama and self-hosted Custom-endpoint models fell through to `price_source: "unknown"` since they're absent from OpenRouter and the bundled table. Ollama node types are now tagged `local` (via `n8n.node.type`) so the waterfall surfaces token usage with a plain "local" tag instead of the ambiguous "price unknown" or a meaningless dollar figure. Custom-endpoint base-URL sniffing is deferred (see spec Non-goals). Shipped — see CHANGELOG v0.4.4.
- [ ] **Expanded notification sinks**: email, PagerDuty, webhook routing per instance
- [x] **Silent-failure detection (green but broken runs)**: catch runs n8n marks success while a node errored under Continue-On-Fail or quietly stopped producing data, the failure class with no failed execution to alert on. On OpenTelemetry ingest it reads output shape rather than status (a normalized demoted-error union plus per-node output-volume-vs-history), suppresses drop cascades to the origin node so one root cause is one alert, and surfaces a distinct `Silent failure` class across the Overview card, Insights, the Observe metrics strip, and the Errors feed. Tunable per instance via `AGD_HEALTH_*`. Shipped (see CHANGELOG v0.5.0). [Architecture note](docs/architecture/silent-failure-detection.md), [spec](docs/specs/2026-07-07-silent-failure-detection.md). Follow-ups:
  - [x] **Dead-man's-switch, layer 1 (a node went missing inside a run that did fire)**: on a completed green run the detector diffs the workflow's declared `workflowData` nodes against the spans that landed, flags a node that had input available but never ran and that historically runs, graph-aware so a legitimate cascade skip is not flagged, gated on run-history for precision (`AGD_HEALTH_DEADMAN_*`). Surfaces in the `Silent failure` class. Shipped (see CHANGELOG v0.5.0).
  - [ ] **Dead-man's-switch, layer 2 (the workflow never fired at all)**: an external heartbeat, since nothing inside n8n can observe its own absence (schedule missed, instance down). Specced, not yet built: [spec](docs/specs/2026-07-11-heartbeat-dead-mans-switch-layer-2.md).
  - [ ] **Configurable expected-output thresholds**: a per-node declared output floor/range so "returned 10, always returns 100" fires explicitly rather than only via the learned drop heuristic. Config on the node, policy defaults roll down from the workspace, and values are suggested from history (one-click accept, only prompting the steady producers that matter) so per-node config scales. Doubles as the per-node override for cases history infers wrong.
  - [ ] **Upstream n8n OTel error semantics** (feature request): get the continued error onto the OpenTelemetry span (standard exception attributes plus span status) so any backend can read it, since n8n currently holds the typed error and then exports the Continue-On-Fail span as OK. Would make detection easier for the whole ecosystem, not just AgeniusDesk. AGD's consumer side is ready: detection prefers the typed `taskData.continuation` rollup a patched n8n records and gates the unsound content-scan behind `AGD_HEALTH_SCAN_LOOSE_JSON_ERROR`, so a patched instance drops the loose-`json.error` false positives. The upstream PR (engine-level continued-error signal) is in review.
- [x] **Trace backfill from execution history** (Phase 1 shipped in v0.6.0; Phase 2 gap-fill open) ([spec](docs/specs/2026-08-14-trace-backfill-from-execution-history.md)): rebuild missing traces from n8n's own execution records, so a receiver outage, a token drift, or an instance wired late is recoverable instead of a permanent hole. n8n stores per-node `startTime` / `executionTime` / status / item counts for every run, which is a 1:1 match for the span shape the waterfall needs, and the API already returns it un-flattened through the same fetch path cost and health enrichment use. Phase 1 is an on-demand rebuild over a range (the recovery case); phase 2 is an opt-in scheduled gap-fill that reconciles anything live export dropped, which makes OTLP ingest best-effort. Bounded by the span retention window in v1.
- [x] **Public run-start API + MCP tools for fleet agents** (shipped in v0.6.0): start any registered fleet agent by id over the versioned public API (API-key auth, trigger scope, shared single-flight semantics with the dashboard), and let vault agents declare `mcp__{server}__{tool}` names (legacy colon form normalized) that resolve to discovered MCP-server tools (bearer via Secrets-store refs); MCP servers can be registered with an explicit slug id so those names are stable across installs. Together with the ticket sink below, this is the agentic-MSP integration seam. Shipped in v0.6.0 — see CHANGELOG.
- [x] **Ticket sink (error groups become PSA tickets)**: a built-in module (off by default) that files one PSA ticket per error group via an itops-mcp tool plane — create on a new group, throttled Internal reply on recurrence, re-arm with a referencing ticket after closure. First-class piece of the agentic MSP loop: the PSA holds the human-facing record, AgeniusDesk holds the grouping and dedup state. Shipped in v0.6.0 — see CHANGELOG.
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

- [x] **`http.request` bridge** (highest leverage): host-mediated outbound HTTP with the credential injected host-side, so a credential-holding module is safe under isolation instead of `in_process`-only. **Shipped in v0.6.0**, with trusted worker identity and read-only endpoint grants ([build spec](docs/specs/2026-09-05-host-http-bridge-build.md)); Proxmox is the first module on it. Unlocks the rest of the REST quadrant — Cloudflare, NocoDB/Baserow/Airtable, Qdrant, object storage, Uptime Kuma, **Home Assistant**, reverse proxy, Pi-hole/AdGuard, Tailscale/NetBird, TrueNAS.
- [ ] **Fleet Health contribution API**: let a loaded module publish `{label, status, metrics}` rows that `fleet_health()` merges, so module health (cluster nodes, queue workers, NAS disks, tunnel status) renders in the Fleet Health pane. The Proxmox module already serves a `/fleet-health` rows endpoint; the remaining work is host-side aggregation of module rows into the pane.

**Homelab Pack v1 core**: Proxmox (**shipped** — first credential-holding community module on the http.request bridge), Remote Docker/Portainer, NAS health (TrueNAS), Uptime Kuma, Cloudflare, Home Assistant. **Extended**: reverse proxy, Pi-hole/AdGuard, Tailscale/NetBird, Authentik. Distributed via the existing bundle mechanism.

The **Redis/queue monitor** and a **database viewer** are wanted but hit the native-wire-protocol wall (no driver delivered by the installer); the DB viewer is better as a built-in. Tracked in the candidates doc.

Not every valuable module folds into the existing chrome; some **add their own surface**:

- [ ] **Support / ticketing module** (opt-in community module, its own UI): a dedicated ticketing view where inbound client support email lands. Agencies running AgeniusDesk field support over a shared address (`support@...`); route that mailbox into the module (IMAP poll or forward-to-webhook) so each thread becomes a ticket in its own inbox, with a status lifecycle (open to resolved), AI triage and draft replies through the assistant, and optional cross-links to the workflow or execution a request concerns. Deliberately **separate from Errors**: error reporting is machine-generated workflow failures, support tickets are human requests; the ticket UI can reference an error but does not live in it. Distributed through the community-module pipeline; pairs with multi-tenancy for per-client routing and feeds the client-reporting loop.

---

## Medium-Term (v0.3+ Concept)

- [ ] **Multi-tenancy foundation**: group instances and workflows by client or team
- [ ] **Audit logging**: track all user actions for compliance (extends the per-install module audit from v0.2)
- [ ] **Cost tracking** — folded into Observability ([cost-observability spec](docs/specs/2026-06-27-cost-observability.md)); LLM spend is the cost dimension of the trace store, not a standalone feature
- [x] **Workflow promotion**: promote workflows across dev, staging, production instances. Shipped in v0.5.0 (`n8n_promote` module: preflight, credential mapping with auto-provision from Secrets, activation guarding; dogfooded end to end on a live instance). [Guide](docs/guide/promote.md)
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
