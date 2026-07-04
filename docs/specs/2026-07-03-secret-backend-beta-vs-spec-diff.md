# Diff: my secret-backend spec vs the shipping beta implementation

Status: REVIEW INPUT. For a stronger model to do a full review on.
Date: 2026-07-03

Context: I wrote `2026-07-03-secret-backend-module.md` as a greenfield design.
Michael then said it is already integrated in the running localhost:3000 build.
That build is `M:\Code\ageniusdesk` (the full/private build; `ageniusdesk-ce` is
the open-source subset). I read the beta and this reconciles it against my spec.

Bottom line: **the beta already implements this, and my spec is the wrong shape in
three material ways (one is outright wrong).** The CE work is a PORT of the beta,
not a build from my design. Details below.

## Sources read (full build)

- `backend/modules/admin/infisical_client.py` — Infisical REST client + `hydrate_env`.
- `backend/modules/vault/client.py` — agent-vault Go-broker client.
- `backend/modules/vault/router.py` — `/api/vault/*` surface.
- `backend/config.py` — `_resolve_secret_ref` (unchanged from CE; no registry).
- `backend/main.py` — boot calls `infisical_client.hydrate_env()`.
- `frontend/js/views/secrets.js` — three-tab UI (Local / Infisical / Agent Vault).

## What the beta actually does

### Infisical = inbound boot-time env hydration (not a live resolver)
`main.py` at startup calls `infisical_client.hydrate_env()` (and again for
`path="/ai"`) BEFORE `migrate_inline_to_secrets`. `hydrate_env` lists all secrets
from Infisical and writes them into `os.environ` with `overwrite=False`, so
container env wins over vault. After that, the existing `_resolve_secret_ref`
resolves `$VAR` from env as normal. Precedence: **container env > Infisical > local
`secrets.json`**, achieved purely by load order, with zero change to
`_resolve_secret_ref`.

Config is via **env vars** read per-call (`INFISICAL_HOST`,
`INFISICAL_MACHINE_IDENTITY_CLIENT_ID/SECRET`, legacy `INFISICAL_TOKEN`,
`INFISICAL_PROJECT_ID`, `INFISICAL_ENV`, `INFISICAL_PATH`). Universal-auth token is
minted and cached to ~60s before expiry. Full CRUD exists (list/create/update/
delete secrets, list folders) and drives the Infisical tab.

### agent-vault = outbound egress broker (values never come back)
`vault/client.py` talks to the upstream Go binary
(`ghcr.io/mfrostbutter/agent-vault:1.0.0`), admin :14321, proxy :14322. Model:

- **Bootstrap:** AGD registers an owner account (fixed email
  `agd-vault-owner@ageniusdesk.com` + operator password) via `POST /v1/auth/register`.
  Session token Fernet-wrapped at `data/.vault_admin_token`; email+password
  Fernet-wrapped at `data/.vault_admin_creds` for re-login after restart.
- **Mirror IN:** `POST /api/vault/refs` resolves a local `$secret` and upserts it as
  a vault **credential** (SCREAMING_SNAKE_CASE, write-only; values never returned by
  the API). Same shape as the n8n credential mirror.
- **Broker OUT:** `mint_scope_token(agent_id, services, ttl)` mints a scoped proxy
  session; the agent subprocess runs with `HTTP_PROXY=http://<token>@vault:14322`
  and the vault injects credentials at egress by matching destination host to a
  **Service** definition. Agents never receive raw keys.
- **Services / Credentials / Audit** management + a request audit log tail.

### UI = three tabs, already built
`secrets.js` is tabbed: Local Store (Fernet `secrets.json`), Infisical (direct
CRUD), Agent Vault (status/bootstrap/services/mirror). Cross-tab actions ("Copy to
Local Store", "Mirror to Agent Vault") are partly visual stubs (`F2`, "not wired").

## Section-by-section diff (my spec → beta reality)

| My spec | Beta | Verdict |
|---|---|---|
| Host `_SECRET_BACKENDS` registry + scheme routing in `_resolve_secret_ref` | None. `_resolve_secret_ref` unchanged | **Drop mine.** Beta needs no host change |
| `$infisical:KEY` live per-ref resolution | Boot-time bulk env hydration → `$VAR` from env | **Beta wins** for simplicity; note tradeoffs below |
| Infisical bootstrap as a compound secret in local store | Env vars read per-call | **Beta.** Simpler ops, rotation without restart |
| agent-vault "Mode A" `$vault:NAME` value resolver | Does not exist by design; vault is write-only | **My Mode A is WRONG.** Delete it |
| agent-vault "Mode B" proxy egress | Implemented (`mint_scope_token` + HTTP_PROXY) | **Beta already ships it** |
| n8n-mirror synergy | Analogous "Mirror to Vault"; Infisical→env→`$VAR` also mirrorable | **Converges** |
| `in_process` + explicit trust consent framing | Runs in-process; no formal consent gate seen | **Keep my framing** as a CE hardening ask |
| New `data/secret_backends.json` connection store | Not used; env vars + `.vault_admin_*` files | **Drop mine** |

## The three material divergences

1. **Resolution model.** Mine: a registry + scheme-routed live resolver, one host
   change. Beta: boot-time bulk hydration into `os.environ`, no host change. Beta is
   simpler and already shipping. Tradeoffs the reviewer should weigh, not silently
   accept:
   - Bulk hydration is a **boot snapshot**. A rotation in Infisical after boot does
     not propagate until the next hydrate/restart (the token cache refreshes, the
     env values do not).
   - **Flat namespace / blast radius.** Every Infisical secret in the project/path
     becomes a process env var, readable by any in-process code and inheritable by
     subprocesses. My scheme-routed model fetched one ref on demand and never
     populated env. This is the main security delta and the thing most worth a hard
     look.
   - No per-ref scoping; `overwrite=False` is the only precedence control.

2. **agent-vault direction was backwards in my spec.** I proposed a value resolver
   (`$vault:NAME` returns a secret to AGD). The vault is deliberately write-only:
   credentials go IN, get injected at the egress proxy, and are never returned. My
   Mode A contradicts the security model and must be dropped. The beta's mirror-in +
   proxy-out is correct and already built.

3. **This is a port, not a build.** My milestones (M1 registry, M2 scaffold, etc.)
   are moot. The real question is the delta to bring the beta's `vault/` module,
   `admin/infisical_client.py`, the boot hydrate hook, and the three-tab `secrets.js`
   into `ageniusdesk-ce`, MIT-clean.

## What my spec still contributes

- **Trust/consent framing.** The beta runs the Infisical + vault paths in-process
  with no explicit install-time consent gate that I saw. For CE, where these may
  ship as community modules, the "this resolves secrets in-process, accept it"
  step is still worth adding. Flag for reviewer: is that needed in CE, or are these
  core built-ins like the beta treats them?
- **n8n-mirror closure.** Worth an explicit test that a hydrated Infisical `$VAR`
  mirrors into a native n8n credential end to end.
- **Optional live per-ref resolution** remains a real future option IF the boot-
  snapshot staleness (divergence 1) becomes a problem. Not needed now.

## Security notes for the reviewer (verify these)

- **`hydrate_env` blast radius.** All Infisical secrets land in `os.environ`.
  Confirm what can read env: `/api/admin/env` filters by prefix (names only), but
  subprocess/agent tiers inherit env unless scrubbed. Is the scrub in place for the
  isolation tiers when Infisical hydration is on?
- **Owner password at rest, reversibly.** `data/.vault_admin_creds` stores the vault
  owner email+password Fernet-wrapped (reversible with `SECRET_KEY`) to allow
  re-login. Accept or redesign (e.g. token-only with manual re-bootstrap)?
- **`INFISICAL_MACHINE_IDENTITY_CLIENT_SECRET` in container env**, not the encrypted
  store. Standard, but note it is visible to anything that can read the process env.
- **`revoke_scope_token` is a no-op** (TTL only). Early revocation of an agent's
  egress token is not possible. Acceptable for the threat model?
- **`mint_scope_token` ignores `allowed_services`** (comment: scoping is via broker
  config, not the session). Per-agent service scoping is therefore weaker than the
  signature implies. Confirm intended.
- **Self-signed TLS** on self-hosted Infisical: confirm the `AGD_TLS_VERIFY` posture
  applies to `infisical_client` (it builds its own `httpx.AsyncClient`; does it honor
  the global verify flag? Looks like it does NOT pass `verify=` — check).

## Port checklist (CE delta)

Files to bring into `ageniusdesk-ce`, MIT-clean:
- `backend/modules/admin/infisical_client.py`
- `backend/modules/vault/` (`client.py`, `router.py`, manifest, bootstrap hook
  `docker_mgr/post_deploy_hooks/agd_vault_bootstrap.py`)
- boot hook in `main.py` (guarded: no-op when unconfigured, which `hydrate_env`
  already handles)
- `frontend/js/views/secrets.js` three-tab version (decide whether to ship the
  stubbed F2 cross-tab actions or cut them)
- config: `vault_admin_url`/`vault_proxy_url` already in CE `config.py`; add the
  `INFISICAL_*` env surface + docs
- tests: `test_agd_vault_bootstrap.py`, `test_secrets_security_boundary.py`,
  `test_subprocess_security_boundary.py`

Open decisions for the reviewer:
1. Core built-in (beta treats it so) vs community module (roadmap said community).
   The in-process secret access argues for built-in; revisit the roadmap stance.
2. Ship the stubbed cross-tab actions or cut to what is wired.
3. Resolve the security notes above before CE exposure, since CE is public and
   self-hosted by strangers, a higher bar than the private beta.

---

# Review findings (2026-07-03 full pass)

Status: REVIEWED. Every security note above was verified against the beta source.
Verdicts below supersede the claims earlier in this doc where they conflict.

## F1 (CONFIRMED BUG): TLS verify flag not honored

Both `admin/infisical_client.py` (all 6 call sites: universal-auth login, list_folders,
list_secrets, create/update/delete) and `vault/client.py` (`_request`, `_login`,
`health`, `status`) construct `httpx.AsyncClient(timeout=...)` with no `verify=`
argument. Every other outbound module (n8n_proxy, proxmox, assistant/mcp_client)
passes `verify=_verify()` driven by `AGD_TLS_VERIFY`. Effect: `AGD_TLS_VERIFY=false`
does not apply to Infisical or the vault admin API, so a self-signed HTTPS Infisical
fails with cert errors. Not a security hole (the default is verify on); it is a
functionality gap and an inconsistency. Fix in the port: share one `_verify()`
helper and pass it everywhere.

## F2 (MAJOR, corrects this doc): "agents never receive raw keys" is NOT true in the beta

The claim at "Broker OUT" above is aspirational, not shipped. Two verified facts:

1. The vault proxy is an HTTP-only MITM with no HTTPS CONNECT support. The env
   builder sets `HTTP_PROXY` only and HTTPS traffic (Anthropic, OpenAI, OpenRouter)
   goes direct (`agents/builder.py` ~line 235; `test_subprocess_security_boundary.py`
   carries an explicit NOTE not to "restore" HTTPS_PROXY). Egress credential
   injection therefore cannot work for any HTTPS API, which is all the ones that matter.
2. Because of (1), `agents/subprocess_runner.run()` resolves the raw secret values
   for `contract.allowed_secrets` via `decrypt_value()` and injects them into the
   subprocess env (`ANTHROPIC_API_KEY` etc). Its own docstring says so. The D1
   grant path concedes the same: `assistant/bindings/grant.py` returns
   `endpoint: ""` with the comment "secrets reach the subprocess via
   subprocess_runner._resolve_secrets_env at spawn time".

What the vault actually provides today: audit logging and gating of plain-HTTP
egress, plus a per-run token whose enforcement surface HTTPS traffic never touches.
The injection is scoped to the contract's `allowed_secrets`, which is real least-
privilege, but it is env-var delivery, not broker delivery.

Also: `test_subprocess_security_boundary.py` asserts on `_build_subprocess_env`
output only. The actual spawned env, after `env.update(_resolve_secrets_env(...))`,
contains secret-shaped keys by design. The "security claim made executable" tests
the wrong layer. For CE: fix the test to spawn-level, or scope its claim honestly.

CE consequence: do not market key isolation for agent subprocesses. Either document
the real model (contract-scoped env injection + HTTP-only audit proxy) or wait for
upstream HTTPS CONNECT support before making the broker claim.

## F3 (CONFIRMED): mint_scope_token ignores allowed_services

`vault/client.py:289`. The parameter is accepted for API compat and dropped; every
token is minted `{vault: "default", vault_role: "proxy"}`. Per-agent service scoping
does not exist; scoping lives only in the vault's global broker config. Combined
with F2 this makes the scope token mostly an audit correlator. Acceptable if
documented; the signature is misleading and should say so in the CE port.

## F4 (CONFIRMED): revoke_scope_token is a no-op

`vault/client.py:311`. Upstream has no revoke endpoint; expiry is TTL-only
(wall-clock limit + 60s). The `finally` revoke in `subprocess_runner` does nothing.
Tolerable given short TTLs; document, and drop the dead call or leave it as a
seam for a future upstream revoke.

## F5 (CONFIRMED): vault owner password at rest, reversible

`bootstrap()` and the already-initialized login fallback both persist email+password
Fernet-wrapped at `data/.vault_admin_creds` (chmod 0600 best-effort; a no-op on
Windows hosts). Anyone with the data dir plus `data/.secret_key` owns the vault.
For CE: at minimum call this out in docs next to the existing secrets.json caveat;
better, offer a token-only mode where credential loss means manual re-bootstrap.

## F6 (VERIFIED, mostly OK): hydrate_env blast radius and the subprocess scrub

All secrets under the configured project/path do land in `os.environ`. But the
subprocess scrub IS in place: `_build_subprocess_env` builds from scratch (only
PATH and PYTHONPATH are inherited), so agent subprocesses do not inherit the
hydrated env. The only secrets that reach a subprocess are the deliberate
contract-scoped injections from F2. In-process code and `/api/admin/*` remain the
exposure surface. Two port-blockers found here:

- `_host()` defaults to `http://infisical.example.lan:8080`, a private LAN host.
  Private infra leak and a wrong default for CE. Default must become empty and
  unconfigured must mean skip.
- `main.py` performs a second hydrate of `path="/ai"` with beta-specific comments
  (agenius pipeline keys). Cut for CE; hydrate the configured path only.

Minor: hydrate logs each loaded secret NAME at info level. Names only, acceptable,
but worth knowing it is in every boot log.

## F7 (NEW, port-critical): the beta has no auth at all

The full build has no auth module and no auth middleware; CORS is `allow_origins=["*"]`.
Every `/api/vault/*` and `/api/admin/infisical/*` route, including bootstrap
(set the vault owner password), mirror (moves plaintext secrets), and rotate
(returns the new secret value in the response), is unauthenticated in the beta.
CE already wraps `/api/*` in the global auth-gate middleware, so the ported routes
inherit login enforcement, but verify during the port that these routes get
admin-role gating (`role_at_least`), not just any-authenticated-user, and that
CSRF applies to the mutating ones. This is the single biggest posture difference
between "fine on Michael's LAN" and "public CE".

## F8 (minor findings)

- `vault/router.py:_resolve_local`: the guard `val == name or val == f"{name}"` has a
  dead duplicate arm; intent was probably `f"${name}"`. Behavior happens to be
  correct because `_resolve_secret_ref` takes bare names and returns the name itself
  on a miss, but clean it up in the port.
- "Local Store" mirror is really env-then-local: `_resolve_secret_ref` checks env
  first, so after Infisical hydration a "local" mirror can silently pull an
  Infisical-hydrated value. Functionally fine; label it correctly in the UI.
- `infisical_rotate` returns the new secret value in the response body (documented
  "return once"). Ensure CE request/response logging never captures bodies on
  this route.
- `_tok_cache` has no lock; concurrent first calls can double-mint a token.
  Harmless, ignore.
- `vault_status` returns the internal admin URL to any caller; behind CE auth this
  is fine.
- `_RefsRequest.source_backend` treats any value other than "infisical" as local;
  validate the enum in the port.

## Verdict on the open decisions

1. Core built-in vs community module: built-in. F2 shows secret material flows
   through core paths (subprocess_runner, config resolution) regardless; a module
   boundary would be cosmetic. Update the roadmap stance.
2. Stubbed cross-tab actions: cut them for CE. Ship wired functionality only.
3. Port gate: F1 (TLS), F6 defaults (LAN IP, /ai path), and F7 role-gating are
   must-fix before CE release. F2 requires honest docs now and is the roadmap item
   (HTTPS CONNECT upstream) if the broker story is wanted for real. F3/F4/F5 are
   document-and-accept.
