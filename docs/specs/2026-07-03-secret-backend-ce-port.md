# Spec: Secret Backends CE Port (Infisical + Agent Vault + Agent Sandbox)

Status: COMMITTED design
Date: 2026-07-03
Owner: Michael Frostbutter

Supersedes `2026-07-03-secret-backend-module.md` (greenfield design, written
before the beta implementation was known) in full. Companion reading:
`2026-07-03-secret-backend-beta-vs-spec-diff.md`, which holds the full review of
the beta source; findings are referenced here as F1..F8 and not restated.

## 1. Goal

Port the beta's secret-backend implementation from the full build
(`M:\Code\ageniusdesk`) into CE, hardened for public self-hosters, and commit a
phased path that makes the Agent Vault isolation claim actually true.

Three user-facing capabilities:

1. **Manage server secrets from the dashboard.** Full CRUD against a
   self-hosted or cloud Infisical instance, plus boot-time hydration so `$NAME`
   refs resolve against it with zero changes to the resolution layer.
2. **Agent Vault for internal agents.** The assistant tool paths and the
   LangGraph/PydanticAI agent fleet get per-run scoped credentials and audited
   egress, phased from "least-privilege env injection" (today) to "keys never
   enter the agent" (Phase 3).
3. **Docker sandbox for agent runs.** Ephemeral, network-jailed containers as
   the enforcement layer that makes 2 sound.

## 2. Decision: built-in, not a community module

Ships in CE core by default. Structural reasons, settled in the review:

- The Infisical hydrate hook runs in `main.py` lifespan before config
  migration. A module cannot own that timing.
- Secret resolution (`_resolve_secret_ref`, `decrypt_value`) lives in
  `backend/config.py`, imported by everything.
- The vault boundary is enforced in `agents/builder.py` and
  `subprocess_runner.py`, which are core agent infrastructure.

The management surfaces (`vault/` router, Secrets UI tabs) follow the normal
module pattern like every other core module.

This updates the roadmap stance (`2026-06-28-integration-modules-roadmap.md`
section 3 said community module; that is now wrong).

### 2.1 Opt-in gating (core but invisible until enabled)

Same philosophy as the agent fleet's `langgraph` extra: the code ships in
core, but a user who never opts in never sees or pays for it. Secret backends
need no extra dependencies, so the gate is configuration plus UI visibility
rather than a pip extra:

- **Enable contract:** Infisical is enabled iff `INFISICAL_*` auth + project
  are set (`is_configured()`); Agent Vault iff `vault_admin_url` is set.
  Unconfigured means zero behavior: `hydrate_env` skips without warnings,
  vault endpoints return a clean "not configured" state, nothing is logged
  beyond one info line at boot.
- **UI:** the Secrets view renders only the Local Store tab by default. The
  Infisical and Agent Vault tabs appear only when the corresponding status
  endpoint reports configured. Users who do not know what Infisical is never
  encounter it.
- **Discoverability:** a single "Connect an external secret backend" link in
  the Local Store tab pointing at the docs page, so the feature is findable
  without being ambient.
- **Compose:** the agent-vault sidecar sits behind a compose profile
  (`docker compose --profile vault up`); `INFISICAL_*` ships as a commented
  block in `.env.example`. A default `docker compose up` runs neither.
- **Agent sandbox interaction:** subprocess agent runs that declare
  `allowed_secrets` require the vault (existing fail-closed behavior in
  `subprocess_runner`). The error message must say how to opt in
  ("enable the vault profile and bootstrap it in Settings"), since with
  opt-in gating this becomes the first vault touchpoint for most users.

## 3. Architecture (as ported)

### 3.1 Infisical: boot-time env hydration + dashboard CRUD

- `backend/modules/admin/infisical_client.py`: thin async REST client,
  universal-auth machine identity (clientId + secret to short-lived cached
  access token; legacy static `INFISICAL_TOKEN` honored). Config read from env
  per-call: `INFISICAL_HOST`, `INFISICAL_MACHINE_IDENTITY_CLIENT_ID`,
  `INFISICAL_MACHINE_IDENTITY_CLIENT_SECRET`, `INFISICAL_PROJECT_ID`,
  `INFISICAL_ENV`, `INFISICAL_PATH`.
- `hydrate_env()` at boot pulls all secrets from the configured project/path
  into `os.environ` with `overwrite=False`. Precedence: container env >
  Infisical > local `secrets.json`, purely by load order. Zero changes to
  `_resolve_secret_ref`.
- Admin router endpoints under `/api/admin/infisical/*`: status, folders,
  list (metadata only, never values), create, update, delete, rotate
  (generates a token, stores it, returns the value once).
- Known tradeoffs, accepted and documented: hydration is a boot snapshot
  (rotation needs restart or manual re-hydrate); hydrated secrets are process
  env vars readable by in-process code. The subprocess scrub (3.3) keeps them
  out of agent runs.

### 3.2 Agent Vault: mirror in, broker out

- `backend/modules/vault/`: `client.py` (async client for the Go agent-vault
  binary, admin :14321, proxy :14322), `router.py` (`/api/vault/*`: status,
  bootstrap, refs/mirror, services, credentials, audit), `service_map.py`
  (AGD secret alias to vault service name).
- Bootstrap registers an AGD-owned account (fixed email, operator password).
  Session token and credentials Fernet-wrapped at `data/.vault_admin_token` /
  `data/.vault_admin_creds` for re-login after restart (F5: reversible at
  rest, documented; token-only mode is a follow-up option).
- Mirror: `POST /api/vault/refs` resolves a secret from Local Store or
  Infisical and upserts it as a write-only vault credential. Values never
  come back out of the vault API.
- Broker: `mint_scope_token(agent_id, services, ttl)` mints a per-run proxy
  token; agent runs get `HTTP_PROXY=http://<token>@vault:14322`.

### 3.3 The honest security model (Phase 1 reality)

Stated plainly because the docs must not overclaim (review F2):

- The vault proxy is HTTP-only MITM. No HTTPS CONNECT. LLM traffic (Anthropic,
  OpenAI, OpenRouter) is HTTPS and goes direct.
- Therefore `subprocess_runner.run()` resolves raw values for the contract's
  `allowed_secrets` and injects them into the subprocess env under the SDK's
  expected names. This is deliberate and stays in Phase 1.
- What IS enforced today: the subprocess env is built from scratch (only PATH
  and PYTHONPATH inherited), so hydrated Infisical secrets and operator shell
  vars never leak into agent runs; injection is scoped to the contract's
  declared aliases; plain-HTTP egress is audited and gated by the vault.
- What is NOT enforced yet: key isolation for HTTPS APIs, per-agent service
  scoping (F3: `mint_scope_token` drops `allowed_services`), early token
  revocation (F4: TTL only).

CE docs and README wording: "contract-scoped credential injection with audited
egress", not "agents never see keys", until Phase 3 lands.

## 4. Hardening deltas (beta to CE)

Must-fix before CE release:

| # | Fix | Source finding |
|---|---|---|
| H1 | Pass `verify=_verify()` (AGD_TLS_VERIFY) in `infisical_client.py` (6 call sites) and `vault/client.py` (`_request`, `_login`, `health`, `status`). Share one helper. | F1 |
| H2 | Remove the hardcoded `http://infisical.example.lan:8080` default for `INFISICAL_HOST`. Unset means not configured, hydrate skips. | F6 |
| H3 | Drop the beta-specific second hydrate of `path="/ai"` in `main.py`. Hydrate the configured path only. If multi-path is wanted later, make `INFISICAL_PATH` comma-separated. | F6 |
| H4 | Admin-role gating (`role_at_least`) on all `/api/admin/infisical/*` and `/api/vault/*` routes; CSRF on the mutating ones. CE's global auth middleware covers login, this adds the role floor. Bootstrap, mirror, and rotate are the sensitive three. | F7 |
| H5 | Fix `test_subprocess_security_boundary.py` to assert on the actual spawned env (post `_resolve_secrets_env`), scoping its claim to "no undeclared secrets" instead of "no secrets". | F2 |
| H6 | Exclude `/api/admin/infisical/*/rotate` (and any route returning secret values) from request/response body logging. | F8 |

Cleanups (do during port, not gates):

- `vault/router.py:_resolve_local`: remove the dead `val == f"{name}"` arm.
- Validate `_RefsRequest.source_backend` as an enum (`local` | `infisical`).
- Label the mirror source honestly in the UI: "local" resolution checks env
  first, so it can pull an Infisical-hydrated value.
- Cut the stubbed cross-tab actions in `secrets.js` (F2 markers, "not wired").
  Ship wired functionality only.
- Document F3/F4/F5 in the module docs as known limitations.

## 5. Phased plan for real isolation

### Phase 1: port as-is (this spec's deliverable)

Subprocess runner + contract-scoped env injection + HTTP audit proxy, with the
section 4 hardening. Works today, honestly documented.

### Phase 2: Docker sandbox for agent runs

Replace the host subprocess with an ephemeral container per run, driven
through the existing `docker_mgr` socket. This is where sandboxing pays off
even before the credential story improves: a prompt-injected or rogue agent
can only reach hosts the vault allows.

Container contract:

- **Network:** attach ONLY to an internal Docker network (`internal: true`)
  where the vault container is the sole member with outbound access. No
  default bridge, no host network. All egress must traverse the vault.
- **Filesystem:** read-only rootfs; the artifact dir is the only writable
  bind mount; no Docker socket inside; agent code mounted read-only.
- **Limits:** memory, CPU, and pids limits from the execution contract;
  existing wall-clock kill retained (stop + remove on timeout).
- **Env:** same scratch-built env as today (`_build_subprocess_env`
  semantics), including Phase 1 credential injection. The jail changes what
  the key can reach, not yet whether the agent holds it.
- **Image:** a slim `agd-agent-runtime` image pinned per release (python +
  pydantic-ai + langgraph + the agd_runtime shim). Built alongside the main
  image, pulled or built at deploy.
- **Fallback:** subprocess mode remains behind
  `AGD_AGENT_SANDBOX=subprocess|docker` (default `docker` when the socket is
  available, `subprocess` otherwise). Nothing breaks for socket-less installs.

For HTTPS under the jail, the vault needs to pass CONNECT tunnels for
allowed hosts (host-allowlist enforcement via the CONNECT target, no payload
inspection). That is a small agent-vault fork feature and the Phase 2
dependency: without it, HTTPS from a jailed container has no route out.

### Phase 3: gateway mode, keys never enter the agent

The HTTPS problem has a clean answer once the agent is network-jailed:

- The agent-vault fork grows a gateway (reverse-proxy) mode per service: the
  agent calls the vault over plain HTTP on the internal network with its
  scope token; the vault injects the real credential and makes the outbound
  HTTPS call. TLS where it matters (vault to provider), injection where it is
  possible (vault-terminated hop).
- Agent side: SDK `base_url` overrides pointed at the vault gateway
  (Anthropic, OpenAI, and OpenRouter SDKs all accept base_url). The
  agd_runtime shim sets these from the resolved bindings; agent code does not
  change.
- Per-service opt-in: services with gateway support drop env injection for
  that alias; anything not yet supported keeps working the Phase 2 way. When
  every alias in a contract is gateway-backed, `_resolve_secrets_env`
  contributes nothing for that run.
- Endgame: delete `_resolve_secrets_env`, flip the docs to the broker claim,
  and make `mint_scope_token` scoping real (per-session allowed services in
  the fork), which also retires F3.

Phase 3 exit test: run an agent with `allowed_secrets=["anthropic_api_key"]`,
dump its env and /proc from inside the container, assert no key material
anywhere, and assert the Anthropic call succeeded through the gateway with an
audit row.

## 6. Port checklist

Files from the full build into CE, MIT-clean:

- `backend/modules/admin/infisical_client.py` (+ H1, H2)
- admin router Infisical endpoints (+ H4, H6)
- `backend/modules/vault/` (`client.py` + H1, `router.py` + H4 + cleanups,
  `service_map.py`, `manifest.json`)
- `main.py` lifespan hydrate hook (+ H3)
- `frontend/js/views/secrets.js` three-tab version, stubs cut, tabs hidden
  unless the backend reports configured (2.1)
- config: `vault_admin_url` / `vault_proxy_url` (already in CE `config.py`),
  `INFISICAL_*` env surface documented in `.env.example` and docs
- `docker-compose.yml`: agent-vault sidecar behind a `vault` compose profile,
  pinned `ghcr.io/mfrostbutter/agent-vault:<tag>`
- tests: `test_agd_vault_bootstrap.py`, `test_secrets_security_boundary.py`,
  `test_subprocess_security_boundary.py` (+ H5), plus a new n8n-mirror
  closure test (hydrated Infisical `$VAR` mirrors into a native n8n
  credential end to end)

Docs to ship with it:

- Secrets guide: three stores, precedence order, rotation caveat (boot
  snapshot), TLS flag behavior, vault owner creds at rest, revocation is
  TTL-only.
- Agent security model page: the honest Phase 1 statement from 3.3, with the
  Phase 2/3 roadmap so the trajectory is public.

## 7. Non-goals

- Not replacing `data/secrets.json`; the local Fernet store stays default and
  fallback.
- No live per-ref Infisical resolution (`$infisical:KEY` scheme). Boot
  hydration is the model; revisit only if snapshot staleness bites.
- No `$vault:NAME` value resolver, ever. The vault is write-only by design.
- No payload-inspecting HTTPS MITM (custom CA in agent images). Gateway mode
  (Phase 3) achieves injection without trust-store surgery.
- Multi-tenant / per-client RBAC on secrets stays on the enterprise roadmap.
