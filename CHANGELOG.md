# Changelog

All notable changes to AgeniusDesk Community Edition are documented here.

## [Unreleased]

## [0.4.4] - 2026-07-06

### Added
- **Scheduled workflow backups.** A new internal interval scheduler runs a best-effort backup job that snapshots every connected instance's workflows to disk under `data/backups/<instance>/`, pruning to a configurable retention. Configure it on the Export / Backup view (enable, interval in hours, snapshots to keep, active-only) or over the new operator-gated `/api/backups` endpoints (`GET/PUT /settings`, `GET` list, `POST /run` to back up now, `GET /{instance}/{file}` download, `DELETE`). The schedule is read live from `config.json`, so toggling it takes effect without a restart, and it fans out across the whole fleet regardless of which instance is active (paginating n8n's cursor so instances with more than 250 workflows back up completely). A failing instance is isolated: the others still snapshot. Off by default.
- **Offsite backup destination (S3-compatible).** Scheduled backups can also push each snapshot to S3-compatible object storage (AWS S3, Cloudflare R2, Backblaze B2, Wasabi, self-hosted MinIO) so a backup survives loss of the host or its Docker volume. Opt-in via a new `s3` dependency extra (minio client; kept out of the default image); enable and configure it under **Offsite destination** on the Export / Backup view, with a **Test connection** probe (`POST /api/backups/test-remote`). The local snapshot is always written first and an upload failure is recorded per instance without losing it. Credentials come only from the encrypted secret store as `$VAR` refs (never stored in config, never returned by the API). Options: mirror the keep-N retention offsite (default on) and Fernet-encrypt the object before upload with the app `SECRET_KEY`. A self-hosted MinIO on the LAN is allowed; the cloud instance-metadata address is refused. Push-only in v1 (restore by downloading the object and using the existing restore drop zone). See [docs/specs/2026-07-06-offsite-backup-s3-sink.md](docs/specs/2026-07-06-offsite-backup-s3-sink.md).

### Changed
- **Local models show token usage instead of "price unknown" in the cost waterfall.** Ollama (and other always-local AI nodes) cost nothing by construction, but the price book had no rate for them so they fell through to the same "unknown" label as a cloud model the book simply hadn't caught up to. Cost enrichment now reads the AI node's `n8n.node.type` off the span and classifies Ollama node types as local (`price_source: "local"`), and the waterfall renders the token counts with a plain "local" tag rather than a dollar figure (the waterfall row shows the total token count in place of a cost). An operator override still pins a real rate if one is set. See [docs/specs/2026-07-02-local-model-cost-clarity.md](docs/specs/2026-07-02-local-model-cost-clarity.md).

## [0.4.3] - 2026-07-05

### Added
- **Python Code nodes work out of the box in deployed n8n.** The built-in n8n template now deploys as a two-container bundle: the n8n main container running task runners in **external mode**, plus an `n8nio/runners` sidecar that ships the JavaScript and Python runners. The stock `n8nio/n8n` image has no Python 3, so the in-process Python runner could not start and Python Code nodes failed; moving execution into the sidecar fixes that and also isolates JavaScript execution (n8n's recommended production posture). The Python **standard library is open by default** (`import json`, `datetime`, `re`, `hashlib`, etc.) via a one-shot init step that patches the runner launcher config into a `-runnercfg` volume and points the launcher at it with `N8N_RUNNERS_CONFIG_PATH`; third-party pip packages still require a custom runners image. A new **n8n version** field tags both images together so their versions always match. The two containers share a minted `N8N_RUNNERS_AUTH_TOKEN` and reach each other over the bundle network. Verified end to end (deploy the bundle, run a Python Code node via webhook). See [docs/guide/containers.md](docs/guide/containers.md#the-n8n-bundle-python-code-node-support).
- `ContainerSpec.init`: bundle members can declare a one-shot init container that runs before the member (sharing its volumes) to seed a config file into a fresh volume. Runs as root by default so it can write into the fresh, root-owned volume.

### Changed
- **Existing template-deployed n8n instances become a two-container bundle on their next redeploy or recreate.** A new `-runnercfg` volume and the `n8nio/runners` sidecar appear, and n8n switches to external runner mode. Your existing workflows, credentials, and data volume are preserved (the per-instance encryption key is reused). No action is required; the change takes effect when you redeploy or recreate the instance.

## [0.4.2] - 2026-07-02

Security release: upgrade recommended. Fixes four high-severity findings from the
2026-07-01 full security review (all reachable by the read-only viewer role or by
an operator escalating to host root), plus the medium/low batch, and ships three
UX additions.

### Security
- **Four high-severity findings from the full security review are fixed** (`docs/code-review/2026-07-01-full-security-review.md`), all reachable by the read-only **viewer** role or by an operator escalating to host root:
  - **Agent Fleet in-process code execution is now admin-only.** Registering or viewing a vault agent imports and executes operator-authored `graph.py` in-process, so the whole `/api/agent-fleet` router was raised from "any authenticated identity" to `require_role("admin")`. There is no safe read subset.
  - **MCP server management SSRF closed.** The `/api/mcp` management router now requires the **operator** role, and every server-side MCP fetch runs its URL through the shared `assert_safe_probe_url` guard (in `_normalize_mcp_urls`), blocking cloud-metadata / link-local / reserved targets while still allowing self-hosted MCP on localhost/LAN.
  - **Docker community-template JSON injection closed.** Template field substitution now runs only on the parsed config object's string leaves, so a field value containing quotes or braces can only land as a string; it can no longer inject `HostConfig` / `Privileged` / bind mounts to reach host root.
  - **DOM XSS in the markdown renderers closed.** The hand-rolled renderers (`assistant.js`, `errors.js`, `codelab.js`) now HTML-escape before their inline transforms and drop non-`http(s)` link hrefs; the Agent Fleet view sanitizes `marked` output with DOMPurify, failing safe to escaped text. This is the path LLM/agent/MCP/RAG output flows through.
  - Covered by regression tests in `tests/test_high_severity_fixes.py` and `tests/test_assistant_authz_ssrf.py`.
- **Medium/Low findings from the same review resolved** (`tests/test_review_medium_low.py`): container `inspect` now redacts secret-looking env values (#5); per-instance secret scope is bound to the instance URL host so a URL repoint can't redirect a scoped secret's mirror (#6); the n8n `test-creds` and credential-mirror paths run the instance URL through the SSRF guard (#7); the dashboard MCP endpoint requires operator+ unless the static token is presented (#8); community templates declaring a host-escaping `HostConfig` (Privileged / host binds / host namespaces / Devices / CapAdd) are rejected at load (#9); the assistant gains a standing prompt-injection guard plus an audit log on every state-changing tool call (#10); the notes search snippet is HTML-escaped (#11); a loud startup warning fires when the OTLP receiver is enabled without a token (#12); API-key hash comparison is constant-time (#13); music mutations and the token-exposing triggers read require operator+ (#14); `AGD_TLS_VERIFY` is honored in the credential-mirror httpx clients (#15); TOTP codes can't be replayed within their window (#17); and `template_state` key material is encrypted at rest (#19).

### Added
- **Name and port your services in the setup wizard, with live conflict warnings.** The stand-up-stack step now shows an **Instance name** and **Host port** field on each selected service (they were previously hidden behind an unbuilt "Advanced" section, so every service silently took its default name and port). As you type a port, the wizard warns inline when it is already bound by a running container (naming the container), is on the browser-unsafe list (ERR_UNSAFE_PORT), or collides with another service you are deploying in the same stack; MinIO's console port and Qdrant's implicit gRPC port (base + 1) are checked too. The Containers deploy panel gains the same "already used by <container>" warning on its port field (it already flagged browser-unsafe ports). Backed by a new `GET /api/containers/ports-in-use` (operator role) returning the published host-port to container map plus the browser-unsafe set, so the pickers can warn before a deploy tries to bind rather than only after Docker fails.
- **Show/hide toggle on password fields.** The auth screens (owner setup, sign in, and password reset) now have an eyeball toggle to reveal the password you are typing, so a typo is caught before submit. The toggle is `type="button"` and out of tab order, so it never submits the form or interrupts keyboard entry, and its label flips between "Show password" and "Hide password" for screen readers.
- **Configurable error-reporting window on the Overview.** A persistent lookback picker (24h / 7d / 30d / 90d / All time, default 30 days), on the Recent Errors widget and in Settings > Error Handler, now drives the whole Overview together: the Recent Errors feed, the Failure Rate card, and the Executions card all report over the chosen window, and the Errors view's range selector uses the same setting. The two execution cards mirror the Insights view (one shared, cached aggregator), so the Overview and Insights always agree: Executions shows the windowed run total with an "N ok, M err, K running" breakdown, and Failure Rate shows n8n's failed-execution rate reconciled against the local error log ("M failed, N in log"), so it is never 0% while errors are listed. The Insights API gained `90d` and `all` ranges, and counting "all" errors no longer narrows to 24h.

## [0.4.1] - 2026-06-30

### Added
- **n8n-only by default; the agent surface is conditional.** The Agent Fleet view and Code Lab's **Agent Builder** mode now appear only when the optional agent extra (`AGD_EXTRAS="...,langgraph"`) is installed, so a default install reads as a pure n8n control plane (the default image already ships without the agent dependencies). `AGD_AGENTS_ENABLED` overrides the auto-detect: `false` hides the agent surface even with the extra present, `true` forces it on. The gate is UI-only (nav + Code Lab mode); it changes no dependencies, and `GET /api/status` now carries `agents_enabled` for the frontend.

## [0.4.0] - 2026-06-28

### Added
- **Agent Fleet — a managed fleet of LangGraph + PydanticAI agents (built-in).** A new core module + sidebar view that operates agents the way AgeniusDesk operates n8n: a catalog (built-ins: ops-triage ReAct, fix-proposer human-in-the-loop, health-reporter parallel fan-out), run with a live graph **and a normalized run waterfall** streamed over the WebSocket, approve/resume for human-in-the-loop, and LangSmith tracing with per-call token/cost. **Both LangGraph and PydanticAI agents run** (PydanticAI via its own adapter); the waterfall renders the same for either, so monitoring is framework-agnostic, and LangGraph additionally shows the node graph. The LangGraph/PydanticAI stack is an **opt-in dependency extra** so the default image stays lean: build with `--build-arg AGD_EXTRAS="assistant,langgraph"` (or `pip install '.[langgraph]'`); without it the module loads but a run reports the missing extra. See `docs/specs/2026-06-28-agent-fleet-langgraph-spec.md`.
- **Delete agents from the fleet.** Each operator-authored (vault) agent card has a **Delete** button that removes its `agents/<id>/` folder after a confirm; the catalog refreshes with no restart. Built-in example agents are protected (no button, rejected server-side) and deletion is blocked during a live run. Cards now also show a framework chip (LangGraph / PydanticAI) and a "built-in" tag.
- **Build your own agents in Code Lab.** Code Lab gains an **Agent Builder** mode (LangGraph | PydanticAI toggle, ReAct / human-in-the-loop / parallel fan-out / blank starters, Python, and agent-aware AI assist in the sidebar). **Register to Agent Fleet** saves the agent into your vault under `agents/<id>/` (a pure `graph.py` factory + an `agent.json` manifest, with a tools picker, model, and human-in-the-loop toggle); the fleet discovers it live with no restart and runs it alongside the built-ins. Agents are vault files you own, so you can edit, export, or delete them. The catalog merges built-ins + your vault agents; vault discovery reads only the manifests, so it stays boot-safe without the langgraph extra.
- **Built-in n8n-mcp (node intelligence), auto-installed.** When Docker is available, AgeniusDesk now starts the [n8n-mcp](https://github.com/czlonkowski/n8n-mcp) server in its own container on first boot and registers it as an MCP server automatically, so Code Lab and the assistant get real n8n node knowledge, search, and workflow validation out of the box. It runs in docs-only mode by default (no n8n credentials needed); a one-click **Upgrade** wires it to the active instance for workflow create/update/manage tools. Surfaced as an **n8n Intelligence** card in Settings → MCP Servers (status, Enable, Wire-to-active-instance, Remove). Best-effort and non-fatal: where Docker isn't reachable it's a one-click **Enable** instead (`POST /api/mcp/n8n-mcp/enable`), and a post-start probe gates registration so a dead endpoint is never registered. The dashboard reaches the container over a published host port via the host gateway (the same path the n8n proxy uses). Opt out with `AGD_N8N_MCP_AUTO=false`; tune with `AGD_N8N_MCP_PORT` / `AGD_N8N_MCP_URL` / `AGD_N8N_MCP_IMAGE`.
- **n8n skill library in the Harness.** The harness now ships a curated library of focused n8n skills under `skills/`, seeded into your vault on first run. Each skill is a `SKILL.md` entry point plus reference docs covering one area (workflow patterns, node configuration, expressions, JavaScript/Python Code nodes, error handling, validation, AI agents, binary/data, sub-workflows, the n8n-mcp tools, multi-instance, and self-hosting), with a router note (`skills/README.md`) the in-app assistant reads first to load the right guidance on demand. Pair it with the n8n-mcp MCP server and Code Lab to build workflows correctly the first time. Seeded once and never overwritten, so your edits stick; opt out with `AGD_SEED_SKILLS=false`. Vendored from [czlonkowski/n8n-skills](https://github.com/czlonkowski/n8n-skills) (MIT), with the license and notices kept alongside the files. The seed `AGENTS.md` now points agents at the library, and the **default Code Lab instructions** route to it (and to the n8n-mcp tools) out of the box: consult the matching skill via the workspace tools, then validate with n8n-mcp before returning a workflow.

### Fixed
- **"CSRF check failed" on mutations after running two dashboards on `localhost`.** Cookies are not isolated by port, so a second AgeniusDesk on another `localhost` port (or its login screen) clears the shared, readable `agd_csrf` cookie for the whole domain. The httponly session survives, so the dashboard stays logged in but every mutation (e.g. switching n8n instances) 403s. The double-submit token is now self-healing: a valid session with a missing `agd_csrf` cookie gets it re-minted on the next `GET /api/auth/status` or `/api/auth/me` (the token is a pure double-submit value, not bound to the session, so re-issuing is safe), and the API client retries a `CSRF check failed` mutation once after re-fetching `/status`, so it recovers with no reload. Tip for local multi-instance dev: use `127.0.0.1` for one and `localhost` for the other to keep their cookie jars separate.
- **Workflow import no longer 400s on a full n8n export.** n8n's public create API is strict (`additionalProperties: false`), so importing a complete export failed with `request/body must NOT have additional properties` whenever it carried a field the importer's denylist missed (e.g. `isArchived` on newer n8n). The importer now builds the create payload from an allowlist (`name`, `nodes`, `connections`, `settings`), and filters the nested `settings` object to n8n's allowed keys too (the same strict rule applies there, e.g. `timeSavedPerExecution` is rejected), so any extra or future export field is dropped and the import succeeds.
- **Deploying a container on a taken host port now warns clearly.** Standing up n8n (or any container) on a port already in use (e.g. an n8n already on 5678) failed with a cryptic Docker bind error. The deploy now pre-checks the requested host port against running containers and fails fast naming the conflicting container, and translates the bind error so a host-process collision is reported plainly instead. The Quick Start also documents changing the dashboard's own port when 3000 is taken.
- **Harness seed docs stay current on existing installs.** The root `README.md` and `AGENTS.md` are seeded only on first run, so improvements (the new `skills/` library, the n8n-mcp pointer) never reached vaults created earlier. They are now refreshed to the current seed on boot **only when still pristine** — an unedited README (content matches a known prior seed) or an unedited constitution (`AGENTS.md` still at version 1 with a known prior body). Any operator edit — via the editor (version bumped) or directly in Obsidian (body changed) — is detected by hash and never overwritten.
- **The dashboard can no longer destroy or stop its own container.** Container management can act on any container via the mounted Docker socket, which meant the dashboard's own container could be destroyed/stopped/recreated from inside the app — taking AgeniusDesk down. It now detects the self-container and refuses destroy, stop, restart, pause, and recreate on it (403, "manage it from Docker Desktop / the host"); the container list flags it (`is_self`) and the UI shows a "this dashboard" marker instead of destructive controls. Harmless actions (logs, inspect, start) and all other containers are unaffected.

## [0.3.0] - 2026-06-28

### Added
- **More AI providers for the assistant and workflow creation.** Beyond OpenRouter / OpenAI / Anthropic / Ollama, the assistant now natively supports **Perplexity, Groq, DeepSeek, Mistral, xAI (Grok), and Together AI**, plus a **Custom (OpenAI-compatible)** provider: set a base URL in Models and point it at any OpenAI-compatible endpoint (Azure OpenAI, LiteLLM, vLLM, LocalAI, Fireworks, ...). They route through the shared OpenAI-compatible chat path with live model listing where the provider exposes a `/models` endpoint; Perplexity is offered tools-free since it rejects an unknown `tools` field. Each area (Code Lab / Error Triage / Assistant) still picks its own provider and model, and keys resolve from the Secrets store by convention (`$PERPLEXITY_KEY`, `$GROQ_KEY`, `$DEEPSEEK_KEY`, `$MISTRAL_KEY`, `$XAI_KEY`, `$TOGETHER_KEY`, `$CUSTOM_LLM_KEY`).
- **VPS deployment guide.** A step-by-step walkthrough for hosting AgeniusDesk as a public web app on your own domain (DigitalOcean, Hostinger, or any Ubuntu VPS): provision, point DNS, run in Docker bound to localhost, and front it with Caddy for automatic HTTPS, plus a public-deployment hardening checklist. Ships `docker-compose.prod.example.yml` (Caddy reverse-proxy overlay) and `Caddyfile.example` so the all-Docker path is copy-paste. See [docs/DEPLOY.md](docs/DEPLOY.md).
- **Community-module frontend isolation (sandboxed iframe).** A community module's frontend view no longer runs in the app page. It loads in an `<iframe sandbox="allow-scripts ...">` without `allow-same-origin`, so the module's code runs in an opaque origin and cannot read or change the host DOM, `window`, cookies, or storage: a buggy or hostile module can break itself but not the AgeniusDesk UI. The module reaches the host only through a `postMessage` bridge that reimplements `window.AgeniusDesk` (`fetch`, `notify`, `navigate`, `openInHarness`); the host verifies the message source and restricts `fetch` to same-origin `/api/` paths (adding auth and CSRF host-side). The host also pushes the active theme's CSS variables into the frame and auto-resizes it to content height. Module code that already uses `AgeniusDesk.*` keeps working unchanged.
- **Community-module backend isolation (out-of-process, opt-in).** A community module's backend can now run OUTSIDE the app process, selected in **Settings > Modules** (or the `AGD_MODULE_ISOLATION` env var, which overrides the setting). Two tiers:
  - **Subprocess** runs each module in a sandboxed child process: host `backend` imports are blocked, the env is scrubbed to an allowlist, and the host reaches it through a reverse proxy with a per-spawn secret.
  - **Container** runs each module in its own hardened Docker container (read-only rootfs, all Linux capabilities dropped, `no-new-privileges`, no Docker socket, pid/memory/cpu limits; modules that declare no network join an internal network with zero internet). This is the real OS boundary.

  Either way, privileged actions go through a loopback **capability bridge**, never direct host access: vault read/write scoped to the module's declared paths (checked against the symlink-resolved location), and a **tool-free `assistant.complete`** that runs the LLM host-side so the provider key never reaches the module. The default stays **in-process**, so existing installs are unchanged. The reference **YouTube Research** module is dual-mode: the same code runs in-process or isolated.

- **Fleet Health view.** A dedicated view aggregating workflow health across every connected n8n instance: per-instance active/total workflows, error rate over recent executions, and the unhealthy workflows, plus a combined roll-up. Live parallel fan-out; a degraded or unreachable instance is shown, not fatal. The "one client becomes ten" pane.
- **Auto-install the error handler on connect.** Adding an n8n instance now best-effort installs + activates the Global Error Handler workflow into it (idempotent), so its errors flow to AgeniusDesk from the moment it's connected. It posts to a container-reachable dashboard URL (`AGD_PUBLIC_HOST`, else a configured host alias). n8n's public API cannot set the instance-wide Error Workflow, so the connect result surfaces that one remaining manual step.

### Fixed
- **Stored XSS in the shared error item (pre-release).** The error renderer shared across Overview, Errors, and Fleet Health escaped an error's `workflow_id` / `execution_id` too weakly for the `onclick` / `href` contexts it writes them into. Since those fields arrive on the login-exempt error webhook, a crafted value could break out of the attribute and run script in the operator's dashboard. The component now escapes the attribute/JS delimiters (matching the source renderer) and percent-encodes ids in URLs; a node-driven regression test renders a hostile error and asserts no breakout.
- **Role floor on error operations.** Error-store endpoints that reach into n8n (purge executions, install/activate the error handler) or clear stored errors now require the **operator** role, matching every other n8n-mutating route; reads and the machine webhook stay open.
- **Error handler vs. webhook token.** The auto-installed Global Error Handler now sends the `x-agd-webhook-token` header (from `$env.AGD_WEBHOOK_TOKEN`), so error delivery keeps working when the dashboard requires a webhook token instead of silently dropping every error.

### Next
- Container tier hardening: drop the module worker to a non-root uid, and a per-host egress proxy that enforces the manifest's declared `network.hosts` (today a network-declaring module reaches any host).

## [0.2.0] - 2026-06-27

### Added

**OpenTelemetry observability**
- Embedded OTLP/HTTP receiver for n8n traces, with token auth (`AGD_OTEL_TOKEN`) and request-body limits. n8n's native OTel exporter speaks HTTP/Protobuf, so the receiver decodes `ExportTraceServiceRequest` directly; no external collector required for the MVP.
- Span storage with bounded retention (age + row cap), pruned on ingest so the trace store stays small on SQLite.
- **Observe** view: a live-updating recent-traces list and a parent/child execution waterfall, plus a per-execution trace popup inside workflow detail.
- Metrics strip (executions, error-rate, p50, p95, throughput) derived from spans, since n8n exports traces rather than OTLP metrics.
- Cross-links: a per-execution **Trace** button in Errors, and a per-workflow "traces" deep-link from Insights into Observe.
- **Cost observability**: LLM spend folded into the trace layer. n8n spans carry no token or cost data, so cost is enriched from n8n run-data (per-call token usage) times a layered price book (OpenRouter-fetched over bundled, estimate-flagged), stored per span and surfaced as a Spend card, a per-trace cost, and a per-AI-span cost in the waterfall.

**Community modules**
- Install third-party modules from a GitHub repo through a two-phase **inspect then install** flow. Inspect pins the exact commit, runs a static AST scan, and lists the module's declared capabilities (network hosts, filesystem write paths, subprocess, env) diffed against what the scan actually detected.
- **Proportional consent**: CRITICAL findings require typing the module id to confirm, HIGH findings require an explicit acknowledgement, and every install records a row in a `module_installs` audit table (who, when, commit, consented capabilities).
- **Monorepo support**: a `discover` endpoint lists every `modules/*/manifest.json` in a repo, and inspect/install take a traversal-safe `path` so one repo can ship many modules.
- One-click **Restart** to activate an installed or removed module (`POST /api/admin/restart`, admin-gated; works under `restart: unless-stopped`).
- Bundled `yt-dlp` so media and transcript modules can extract captions in-process (no GPU, no sidecar). First consumer: the **YouTube Research** community module, distributed from a separate repo and installed through this flow.

**Harness**
- Deep-link to open any vault path from anywhere in the app (`window.__harnessOpenPath` / `AgeniusDesk.openInHarness`); opening a note reveals it in the tree (expands ancestors, scrolls to and highlights the file) instead of dumping you at the root.

**Release hygiene**
- Logout control in the app chrome (sidebar account row).
- Persistent Code Lab across instance switch: the editor buffer survives re-render, so authoring on one instance and deploying to another no longer loses work.
- "Open" button per instance in the sidebar switcher: open an n8n instance's UI in a new tab.

- New built-in **n8n** dark theme, styled after the n8n product (solid neutral-gray surfaces, orange accent, teal-green success). Brings the built-in theme count to three (Dark, Light, n8n).
- Instances, Models, and MCP are now first-class sidebar views instead of deep-links into Settings. Clicking them shows a focused, single-purpose page (no Settings tab strip); the same panels still live under the Settings gear as tabs. This also fixes the wrong (Settings) coachmark firing on those pages.
- Page coachmarks now cover every primary view: a single orienting bubble on Overview, Workflows, Executions/Errors, and Containers, plus dedicated tours for Instances, Models, and MCP. The Code Lab tour now calls out the Prompt Builder.
- Security hardening: internal `/api/*` routes now have a central auth gate, edge-auth headers are trusted only with `AGD_TRUST_EDGE_AUTH=true`, legacy ingest webhooks can be protected with `AGD_WEBHOOK_TOKEN`, and external dashboard MCP clients can use `DASHBOARD_MCP_TOKEN`.
- Test suite (`tests/`): first automated regression coverage, pinning the security-hardening behaviors that had no other safety net. Covers the fail-closed internal-API middleware (public allowlist passes, private routes 401 without identity and pass with an admin token), edge-auth trusted only when `AGD_TRUST_EDGE_AUTH=true`, legacy webhook token enforcement (bearer + `X-AGD-Webhook-Token`), theme- and JS-path traversal guards, and the no-account-enumeration property of password recovery. Run with `uv run pytest`.

### Fixed
- Observe: the trace detail no longer stays pinned at the top when you select a workflow from far down a long list. The detail panel is sticky and the selected trace scrolls into view.
- Observe: repaired double-encoded UTF-8 (mojibake) in span and workflow names on ingest (a cp1252 round-trip), so titles with em dashes and other punctuation read correctly; existing rows were backfilled.
- Community modules: serve a module's static assets over `HEAD` as well as `GET`. The frontend loader probes `module.js` with `HEAD` before loading it, so a `GET`-only route silently left community views blank.
- Models: each area (Code Lab / Error Triage / General Assistant) now has an **API key** dropdown listing your stored secrets, so a key saved under any name (e.g. `$OPEN_ROUTER_API_KEY`) can be selected directly. Previously the area only resolved a single hard-coded convention name (`$OPEN_ROUTER_KEY`) with no way to point at a differently-named secret. Leaving it on "Use provider default key" keeps the convention behavior. Key resolution at chat time honors the per-area selection.
- Models: selecting an API key now **tests the connection and reloads the live model list using that key**, so you see every model the key can reach instead of the short hardcoded fallback (e.g. OpenRouter jumps from ~11 fallback entries to the full live catalog). The `/models` and `/test-creds` endpoints accept a secret ref and resolve it server-side (the plaintext key never returns to the browser); the model cache is keyed per-key so areas with different keys don't evict each other.
- Models: each of the three areas (Code Lab, Error Triage, General Assistant) has its own **Test & load models** button, so every area/provider can be validated and its live model list pulled independently. The OpenRouter connection test now hits the key-validation endpoint (its model list is public and returned 200 even for bad keys), so an invalid key is correctly reported.
- Models: each area now has its own **Save** button that persists just that area (partial save), replacing the single floating "Save all areas" bar.
- Models: testing an area with "Use provider default key" when no key is configured now returns a clear "No API key set" message instead of crashing with `Illegal header value b'Bearer '` (an empty key was being sent as an empty `Bearer` header). The default-key test also resolves the per-provider convention secret.
- The post-wizard "Connect your n8n" guide no longer collides with a dashboard coachmark: the Overview/dashboard bubble was removed (the dashboard is self-explanatory and is where the get-started card and connect guide already live).
- Dashboard "command center" tip anchors on the always-present "+ Widget" control instead of the widget grid, which is empty (zero-height) until widgets load async and would otherwise mark the tip seen before it ever showed.
- CSRF protection regressed any action using a raw `fetch()` that bypassed the API helper (workflow delete / delete-archived, container lifecycle, music player) — they returned "failed" because the CSRF token wasn't attached. A global `fetch` shim now adds the token to every same-origin mutation, covering current and future callers.
- Error-handler install is idempotent: it reuses an existing "Global Error Handler" workflow instead of importing a duplicate, and the post-connect prompt detects an already-installed handler and just confirms it.
- Static/theme path handling now resolves paths under the intended frontend or `data/themes` directory before reading or writing, closing traversal edge cases in custom asset handlers.

**Viewport, onboarding polish, and password policy**
- Site-wide horizontal-overflow guard: nothing produces a horizontal scrollbar. Fixed the coachmark spotlight overshooting the viewport (it now cancels the app's body `zoom` so it maps 1:1 to the screen).
- Page coachmarks lead with what each area is for. The discovery-heavy workspaces (Code Lab, the Harness, MCP, Models, Secrets) get a short multi-step walkthrough; the self-explanatory list views (Overview, Workflows, Errors, Containers) get a single orienting bubble.
- Stronger password policy: 12+ characters with an uppercase, lowercase, number, and symbol (each class configurable via `AGD_PASSWORD_REQUIRE_*`). Setup, reset, and change-password show a live requirements checklist; the same rules are enforced server-side.
- Setup wizard: the "stand up my stack" path no longer skips straight to the end. After the stack deploys it continues through Secrets and AI Assistant, then the dashboard shows a guided "connect your n8n" prompt (open n8n, create the account, mint an API key, register it). The "Sync to n8n" step was removed entirely, and the step indicator now shows only the steps that apply to your chosen path.
- Docker networking: when the dashboard runs in a container, an n8n URL of `localhost` is transparently reached via `host.docker.internal` (stored as the backend URL while the browser link keeps `localhost`), so connecting a host-published n8n no longer fails with "connection refused." The connect form also pre-fills the URL with the host you reached the dashboard by, so accessing AgeniusDesk via a LAN IP auto-fills that IP.

**Onboarding and page tips**
- Setup Journey "Get started" card on the Dashboard. Milestone completion is derived live from app state (n8n connected, secrets added, AI configured, 2FA on, harness visited), so it stays honest and resumable instead of tracking a stored step. Auto-hides once core setup is done; dismissible and reopenable from Settings.
- Page coachmarks: a dependency-free spotlight + bubble walkthrough that runs the first time you open a view, pointing out the key controls (Dashboard, Workflows, Errors, Code Lab, Knowledge, Secrets, Settings, Containers). Tolerates missing anchors, honors `prefers-reduced-motion`, and is keyboard and screen-reader friendly.
- Settings > Help & Tips: toggle page tips, replay the current page's tour, reset all tips, and reopen the setup checklist or wizard. Persistence is per-browser (localStorage).

**Authentication, accounts, and 2FA**
- Built-in login with local accounts. First browser visit forces creation of an owner account (admin), then requires sign-in. The owner account is keyed by **email** (the login identity, also used for recovery); the setup screen spells out the password requirement. Edge identity (Cloudflare Access) can satisfy the gate only when `AGD_TRUST_EDGE_AUTH=true`, and the `AGD_ADMIN_TOKEN` bearer can satisfy the gate when configured.
- Forgot-password flow: a "Forgot password?" link issues a single-use, time-limited reset link to the account email (no account-enumeration in the response). Completing a reset invalidates all other sessions. Delivery uses SMTP (`AGD_SMTP_*`); when SMTP is unconfigured the link is logged so self-hosted installs without a mail server can still recover access.
- Auth bootstrap endpoints (`/setup`, `/login`, `/forgot`, `/reset`) are exempt from the CSRF gate, fixing a first-run failure where a stale `agd_session` cookie (left after a data-volume wipe, or shared across localhost ports) blocked account creation with "CSRF check failed."
- Optional TOTP two-factor (any authenticator app), with one-time recovery codes. QR rendered client-side from a vendored, dependency-free generator; the setup key is also shown for manual entry.
- Server-side sessions stored as a SHA-256 of the token (a DB leak cannot be replayed), `HttpOnly` + `SameSite=Strict` cookie, sliding expiry with an absolute cap, server-side revocation, and a session list in Settings > Account.
- Double-submit CSRF protection on cookie-authenticated mutations.
- Coarse role-based access control (`viewer < operator < admin`): admin/secrets require `admin`; the n8n and container control surfaces require `operator`; read surfaces require any signed-in user. Machine webhooks (errors, messages) stay open for n8n ingestion.
- Password hashing raised to PBKDF2-HMAC-SHA256 at 600k iterations with login-time rehash of legacy hashes; minimum password length raised to 12.
- New settings: `AGD_DISABLE_LOGIN`, `AGD_SESSION_TTL_DAYS`, `AGD_SESSION_ABSOLUTE_DAYS`, `AGD_LOGIN_MAX_ATTEMPTS`, `AGD_LOGIN_LOCKOUT_MINUTES`, `AGD_PASSWORD_MIN_LENGTH`, `AGD_PASSWORD_REQUIRE_*`, `AGD_PASSWORD_RESET_TTL_MINUTES`, `AGD_TRUST_EDGE_AUTH`, `AGD_TRUST_FORWARDED_FOR`, `AGD_WEBHOOK_TOKEN`.
- `harden_file_permissions()` now also `chmod 600`s `users.json` and `dashboard.db`.

## [0.1.0] - 2026-06-23

### Initial Release

The first open-source, MIT-licensed release of AgeniusDesk Community Edition.

#### Features

**Multi-Instance Management**
- Add unlimited n8n instances by URL and API key
- Switch between instances instantly
- Encrypted credential storage with Fernet (AES-128-CBC + HMAC-SHA256)
- Secret references via `$VAR_NAME` syntax

**Error Visibility**
- Real-time error feed with WebSocket streaming
- Errors grouped by workflow, node, and error type
- Occurrence counts and last-seen timestamps
- Global error handler workflow for seamless integration

**Workflow Management**
- List, search, activate/deactivate workflows
- Trigger workflows on demand
- View execution history per workflow
- Import, export, and backup workflows

**Code Lab**
- Monaco editor for n8n Code-node JavaScript/TypeScript/Python
- AI assistance for code generation and explanation
- Syntax highlighting and n8n node introspection
- One-click "Send to n8n" deployment

**AI Assistant**
- Support for OpenRouter, OpenAI, Anthropic, and local Ollama
- Function calling to query workflows, manage executions, and analyze errors
- MCP server integration for extending with external tools
- Optional RAG via Qdrant for knowledge-based context
- Custom system instructions and knowledge file uploads

**Knowledge Management**
- External knowledge source registration
- Markdown notes vault compatible with Obsidian
- Full-text search with BM25 ranking
- Tag-based organization, backlinks, and wikilinks
- Folder tree structure for note organization

**Container Management**
- List, inspect, and manage Docker containers
- One-click container deployment with built-in templates
- Community template library (drop JSON into `data/templates/`)
- Container lifecycle actions (logs, recreate, destroy)
- Multi-container bundle support with shared networking

**Secrets Store**
- Fernet-encrypted credential storage at rest
- Environment variable resolution with encrypted fallback
- Reference secrets as `$VAR_NAME` anywhere in the app

**Notifications**
- Inbound webhook for dashboard messages (`/api/messages/webhook`)
- Messages persisted and broadcast to all open tabs as toasts
- Generic `SLACK_WEBHOOK_URL` / `DISCORD_WEBHOOK_URL` env vars for operator-supplied sinks (no keys hardcoded)

**Insights & Analytics**
- Execution success rates and error trends
- Per-instance health status tracking
- Workflow busiest / slowest statistics

**Themes & Customization**
- 2 built-in themes (Dark, Light) plus custom theme support
- Custom theme support via JSON
- Music player integration (Spotify, YouTube, SoundCloud, Apple Music, Tidal)

**Deployment & Operations**
- Single-command Docker Compose setup
- Bare-metal Python 3.10+ installation supported
- TLS verification gating via `AGD_TLS_VERIFY`
- Optional authentication (`AGD_REQUIRE_AUTH=true`)
- Comprehensive configuration via environment variables

#### Architecture

- Python 3.10+ FastAPI backend with async I/O
- Vanilla JavaScript frontend with zero build step
- SQLite default storage (PostgreSQL optional)
- Auto-discovered module system for extensibility
- WebSocket server for real-time updates
- Public API at `/api/v1` with X-API-Key authentication

#### Documentation

- README with quickstart guide
- Full API reference
- Deployment runbook for self-hosting
- Configuration reference
- Contributing guidelines

#### Removed (from the internal fork this release is derived from)

Stripped to keep the open-source tree generic and free of operator-specific wiring:

- Outbound notification-router module that was hard-wired to private Slack/Discord
  channels and event classes (morning brief, analytics digest, P0/backup alerts).
  Outbound notifications are now just the generic `SLACK_WEBHOOK_URL` /
  `DISCORD_WEBHOOK_URL` env vars; the inbound message bus is unchanged.
- `metric_snapshots` and `agent_runs` database tables (internal analytics cache and
  agent-run telemetry); neither had any reader or writer in the Community Edition.
- Operator-specific references in module metadata and code comments (private vector
  collection names, internal workstation/host names).
