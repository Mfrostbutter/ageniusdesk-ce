# Roadmap

AgeniusDesk Community Edition is a lightweight, open-source control plane for n8n. The roadmap prioritizes stability, extensibility, and the features operators need most.

Specs for in-progress and planned work live in [`docs/specs/`](docs/specs/).

## Current Release: v0.2.0 (2026-06-27)

v0.2 lands full execution observability, the community-module install pipeline and its first module, and the authentication and onboarding layer, all on top of the v0.1 core. Highlights:

- **OpenTelemetry observability**: embedded OTLP receiver, the Observe trace waterfall, a metrics strip, and LLM cost enrichment folded into the trace layer
- **Community-module pipeline**: inspect / scan / consent install flow, monorepo discovery, a per-install audit trail, and one-click restart
- **YouTube Research module**: the first community module (captions to a structured breakdown, auto-filed into the notes vault)
- **Authentication and onboarding**: owner account, session login, optional TOTP, password reset, RBAC, CSRF, and per-view coachmarks

Full detail and checkboxes are under "What shipped in v0.2.0" below; see the [CHANGELOG](CHANGELOG.md) for the complete entry.

## Previous Release: v0.1.0 (2026-06-23)

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
- [x] **Cost observability** ([spec](docs/specs/2026-06-27-cost-observability.md)): LLM spend folded into the trace layer. n8n's spans carry no token/cost data, so cost is enriched from n8n run-data (per-call token usage) x a layered price book (OpenRouter-fetched > bundled, est-flagged), stored per span, surfaced as a Spend card, per-trace cost, and per-AI-span cost in the waterfall. Verified live (a Sonnet agent run priced at ~$0.34). Subsumes the old "Cost tracking integration" item. Follow-ups: operator price overrides UI, the cost-aware gateway for exact cache-aware cost.

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

- [ ] **More container templates**: MySQL, MongoDB, Minio, additional databases and services
- [ ] **Richer Code Lab**: code snippets library, n8n node documentation sidebar, template expansion
- [ ] **Additional knowledge connectors**: HTTP fetch, GitHub, API connectors beyond Qdrant
- [ ] **Harness skills section**: a library of skills in the Harness that agent instructions can point at for specific areas of concern, so an agent loads focused, domain-specific guidance on demand
- [ ] **Curate high-quality n8n skills**: source and vet excellent n8n-focused skills (node config, expressions, workflow patterns) to ship as starting content for the Harness skills section
- [ ] **Workflow version history**: snapshot on import, diff viewer, restore from snapshot
- [ ] **Scheduled backups**: automated per-instance backup with configurable retention
- [ ] **Health monitoring**: configurable endpoint polling, uptime tracking, SLA dashboards
- [ ] **Expanded notification sinks**: email, PagerDuty, webhook routing per instance
- [ ] **Workflow security audit scan**: detect missing error handlers, unused credentials, exposed webhooks (this audits n8n workflows; distinct from the community-module code scanner in v0.2)
- [ ] **Project landing page**: a public web page introducing AgeniusDesk CE (overview, screenshots, install, docs and repo links)

---

## Next Release (v0.3) — In Progress

The headline is real isolation for community modules, the boundary the v0.2 scan/consent layer bridges:

- [ ] **Frontend iframe isolation**: render each community view in a sandboxed `iframe` (`allow-scripts`, no `allow-same-origin`) with a postMessage RPC bridge to a whitelisted host API (`fetch` / `notify` / `navigate` / `openInHarness`), plus theme propagation and auto-resize. Today a module's frontend is injected into the app page and can read, change, or break the host UI; the iframe removes that.
- [ ] **Out-of-process backend isolation**: run a module's Python in a sandboxed subprocess behind an RPC contract, so a module no longer runs in-process with full data and credential access.

## Medium-Term (v0.3+ Concept)

- [ ] **Multi-tenancy foundation**: group instances and workflows by client or team
- [ ] **Audit logging**: track all user actions for compliance (extends the per-install module audit from v0.2)
- [ ] **Cost tracking** — folded into Observability ([cost-observability spec](docs/specs/2026-06-27-cost-observability.md)); LLM spend is the cost dimension of the trace store, not a standalone feature
- [ ] **Workflow promotion**: promote workflows across dev, staging, production instances
- [ ] **Public API hardening**: expand and stabilize the existing versioned `/api/v1` (X-API-Key) surface

---

## Future Directions

- Module isolation (frontend iframe + out-of-process backend) is the real security boundary; tracked under Next Release (v0.3) above
- Whisper transcription fallback for the YouTube research module (videos without captions; never a bundled GPU dependency)
- Workflow diff viewer (visual side-by-side comparison)
- External secret sources (1Password, AWS Secrets Manager, Vault)
- Git integration (export workflows to repos, branch-based environments)
- SAML/LDAP for team authentication
- Agentic workflow management (LangChain integration, agent monitoring)
- Client-facing portal (scoped workflow access for non-operators)
- Home Assistant integration for homelab automation centers
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
