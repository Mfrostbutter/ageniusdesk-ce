# Spec: Secret-Backend Module (Infisical + agent-vault)

Status: SUPERSEDED by `2026-07-03-secret-backend-ce-port.md` (2026-07-03).
Written as a greenfield design before the beta implementation was known; the
beta already ships this differently. See
`2026-07-03-secret-backend-beta-vs-spec-diff.md` for the reconciliation and
review. Kept for the trust/consent framing and the n8n-mirror closure test,
both carried into the port spec.
Date: 2026-07-03
Owner: Michael Frostbutter

Supersedes the "secret backends" scoping in
`2026-06-28-integration-modules-roadmap.md` section 3 and the "inverted-bridge"
open question in `2026-06-28-community-module-candidates.md`. This doc commits the
design those two flagged. Read those first for the landscape; this is the how.

## 1. Goal

Let AgeniusDesk resolve `$NAME` secret references from an external broker in
addition to the local encrypted store. Two backends:

- **Infisical** (cloud or self-hosted), machine-identity Universal Auth.
- **agent-vault** (Infisical-compatible credential broker already running as a
  sidecar), both as a value source and as a proxy broker for Agent Fleet egress.

Principle from the roadmap, kept: extend the existing resolution layer, do not
replace it. The local Fernet store stays the default and the fallback.

## 2. Non-goals

- Not replacing `data/secrets.json`. Local store remains first-class.
- Not writing broker secrets to disk in plaintext, ever. Broker values live in
  memory with a TTL and are gone on restart.
- Not a general external-secrets governance layer (per-client RBAC, audit
  receipts). That stays on the enterprise roadmap.
- Not the n8n community node. That idea is dropped; see
  `../specs/` history if needed. This lives host-side.

## 3. The core decision: inverted module, host-side resolver

A secret backend inverts the normal module model. The capability bridge is built
for a sandboxed worker to call the host (`assistant.complete`, `notes.*`, and the
planned `http.request`). A secret backend is the opposite: the **host** must pull
secret values **from** the backend, inside `decrypt_value()`, which runs deep in
host code on every resolution.

Therefore this module does not run as a sandboxed worker. It registers a
**host-side resolver** into a new backend registry in `config.py`. Consequences,
stated honestly:

- It runs `in_process`. It holds the broker bootstrap credential and participates
  in host secret resolution. Sandboxing would scrub the env and defeat the purpose.
- It ships as a **community module** but installs behind an explicit **trust
  consent** step (the "consented-secret tier" from the roadmap's option 2, combined
  with the "host-side resolver" option 3). The installer's inspect/scan/consent
  pipeline must show that this module registers a secret resolver and runs
  in-process, and the operator must accept that before it mounts.

This is the secret-backend analogue of the `http.request` bridge decision: one
small, well-scoped host extension point unlocks the whole capability, rather than
bending the module into a shape that does not fit.

## 4. Host change: the secret-backend registry

The only host-side code change. Everything else lives in the module.

Today `_resolve_secret_ref(name)` in `backend/config.py` does env then
`secrets.json`. Add a registry that lets a backend claim a URI scheme prefix.

```python
# backend/config.py  (new)

# scheme -> resolver(rest: str) -> str | None
# A resolver returns the plaintext value, or None if it does not have that ref.
_SECRET_BACKENDS: dict[str, "SecretBackend"] = {}

class SecretBackend(Protocol):
    scheme: str                       # e.g. "infisical", "vault"
    def resolve(self, rest: str) -> Optional[str]: ...

def register_secret_backend(backend: "SecretBackend") -> None:
    _SECRET_BACKENDS[backend.scheme] = backend

def unregister_secret_backend(scheme: str) -> None:
    _SECRET_BACKENDS.pop(scheme, None)
```

Hook it into `_resolve_secret_ref` before the local lookup. Secret names are
UPPER_SNAKE and never contain a colon, so `scheme:rest` is unambiguous:

```python
def _resolve_secret_ref(name: str) -> str:
    # NEW: scheme-routed refs go straight to the registered backend.
    if ":" in name:
        scheme, rest = name.split(":", 1)
        backend = _SECRET_BACKENDS.get(scheme)
        if backend is not None:
            val = backend.resolve(rest)
            if val is not None:
                return val
            # Explicit scheme that misses stays unresolved. Do NOT fall through
            # to the local store; that would mask a broker misconfiguration.
            return f"${name}"
    # ... existing env-then-secrets.json behavior unchanged ...
```

Optional fallthrough (off by default): when
`AGD_SECRET_BROKER_FALLTHROUGH=true`, a bare `$NAME` that misses env and the local
store is tried against the default broker connection. Env always wins first, so an
operator can still override a brokered value in an emergency. Off by default keeps
resolution predictable and network-free for existing installs.

This registry is the entire host surface. It is generic: any future backend
(HashiCorp Vault, AWS Secrets Manager) registers the same way.

## 5. Reference syntax

| Form | Resolves to |
|---|---|
| `$NAME` | Local store / env (unchanged). Optional broker fallthrough. |
| `$infisical:KEY` | `KEY` from the default Infisical connection (its configured project + environment + path). |
| `$infisical:project/env/path/KEY` | Fully qualified override. `path` may contain slashes; last segment is the key. |
| `$vault:NAME` | `NAME` from the agent-vault broker. |

Short form is the ergonomic default so most refs read `$infisical:STRIPE_KEY`.
The qualified form is the escape hatch for a second project or environment.

## 6. Caching and security

- Resolved broker values are cached **in memory only**, keyed by full ref, with a
  TTL (`AGD_SECRET_BROKER_TTL`, default 300s). Never persisted.
- The broker **bootstrap credential** (Infisical machine identity, vault token) IS
  stored, in the existing local Fernet store as a compound secret, and referenced
  by the connection config. We bootstrap the external store from the local one.
- The resolver never logs a resolved value. Errors log the ref and status, not the
  secret.
- Cache is process-local. A restart re-fetches. A manual "flush cache" action is
  exposed for rotation.
- Broker calls respect `AGD_TLS_VERIFY` like every other outbound httpx call.

## 7. Backend 1: Infisical

Pure REST over the bundled httpx. No SDK (keeps the module dependency-free, same
reasoning as elsewhere in the codebase).

### Connection config
Stored as a new compound secret template `infisical` so onboarding reuses the
existing Secrets UI (add to `backend/modules/admin/secret_templates.py`):

| Field | Secret | Notes |
|---|---|---|
| `baseUrl` | no | `https://app.infisical.com` or self-hosted origin |
| `clientId` | no | Universal Auth machine identity |
| `clientSecret` | yes | |
| `projectId` | no | default project (the `workspaceId`) |
| `environment` | no | default env slug, e.g. `dev` |
| `secretPath` | no | default path, `/` |

The module's connection record points at this secret by name, so the resolver
reads `$INFISICAL_CONN.clientSecret` etc. through the existing compound-secret
machinery. No new crypto.

### Auth + resolve
1. Universal Auth login: `POST {baseUrl}/api/v1/auth/universal-auth/login`
   `{clientId, clientSecret}` -> `{accessToken, expiresIn}`. Cache the token in
   memory until `expiresIn` minus a safety margin.
2. Resolve one key: `GET {baseUrl}/api/v3/secrets/raw/{KEY}?workspaceId={projectId}
   &environment={env}&secretPath={path}` -> `{secret: {secretValue}}`. Return
   `secretValue`.
3. Optional: list for the browse UI: `GET {baseUrl}/api/v3/secrets/raw?...`.

`workspaceId` is the project ID. Verify exact field names against the running
Infisical version before locking (test target: self-hosted Infisical on the LAN).

## 8. Backend 2: agent-vault

agent-vault already exists in-tree: `vault_admin_url` (:14321 admin REST + web UI)
and `vault_proxy_url` (:14322 MITM egress proxy), docker-compose `agents` profile,
already consumed by `agent_fleet`. Two modes.

### Mode A: value resolver
`$vault:NAME` -> the vault admin REST API returns the secret value host-side, same
shape as the Infisical backend. Since agent-vault is Infisical-compatible, the
resolver may share most of the Infisical client code behind a small adapter.

### Mode B: proxy broker (the Agent Fleet payoff)
The higher-value mode, and why agent-vault matters beyond "another Infisical."
Agents in the Fleet run under container isolation and must never see raw keys. The
vault's :14322 proxy injects credentials into outbound requests at the egress hop.
So instead of resolving a value into the agent, we hand the agent
`HTTP_PROXY=vault_proxy_url` and the vault attaches the auth. The agent orchestrates;
the key never enters the worker. This is the same philosophy as the bridge, enforced
at the network layer.

This mode is a config wiring, not a resolver: the Fleet runner sets the proxy env
on the worker when a vault connection is present and the agent's manifest opts in.
Spec the manifest flag (`egress_broker: "vault"`) as a follow-on to the Fleet spec;
this module owns the vault connection + admin API, the Fleet owns the wiring.

## 9. Module layout

Community module, same shape as `youtube-research`, lives in
`ageniusdesk-community-modules/secret-backends/`.

```
secret-backends/
  manifest.json          # id, routes_prefix, trust: {in_process, registers_resolver}
  __init__.py            # exposes router; on import, registers backends
  backends/
    base.py              # SecretBackend protocol impl, in-memory TTL cache
    infisical.py         # UA login + raw secret fetch
    vault.py             # admin REST adapter (+ proxy-broker connection info)
  router.py              # connect/test/list/flush endpoints
  connections.py         # data/secret_backends.json (connection records, no secrets)
  frontend/
    secret-backends.js   # connect + browse view
```

`manifest.json` additions beyond the standard fields:
```json
{
  "id": "secret_backends",
  "routes_prefix": "/api/secret-backends",
  "trust": { "isolation": "in_process", "registers_secret_resolver": true },
  "capabilities": ["secret-resolver"]
}
```
The installer's consent screen reads `trust` and surfaces "this module resolves
secrets in-process" as an explicit accept.

### Registration
On module import, for each configured connection, instantiate the backend and call
`config.register_secret_backend(...)`. On connection delete, `unregister`. Boot is
best-effort: a broker that is unreachable at startup registers anyway and returns
None (unresolved) until it recovers, so a down broker never crashes resolution.

## 10. Connections data model

`data/secret_backends.json` holds connection records only, never secret material:

```json
{
  "connections": [
    {
      "id": "inf_ab12",
      "type": "infisical",
      "scheme": "infisical",
      "label": "self-hosted",
      "bootstrap_secret": "$INFISICAL_CONN",
      "default": true
    },
    {
      "id": "vlt_cd34",
      "type": "vault",
      "scheme": "vault",
      "label": "agent-vault",
      "admin_url": "http://vault:14321",
      "proxy_url": "http://vault:14322",
      "bootstrap_secret": "$VAULT_TOKEN"
    }
  ]
}
```

`bootstrap_secret` is a `$ref` into the local store, so the file itself carries no
credential. `default: true` marks which connection short-form `$infisical:KEY`
uses.

## 11. API surface

`/api/secret-backends`, operator role floor (resolves and reaches brokers):

| Method | Path | Purpose |
|---|---|---|
| GET | `/connections` | list connections (no secret material) |
| POST | `/connections` | add a connection; references a bootstrap `$ref` |
| POST | `/connections/{id}/test` | login + fetch a probe key; returns ok/error |
| DELETE | `/connections/{id}` | remove + unregister backend |
| GET | `/connections/{id}/keys` | browse available keys (names only) |
| POST | `/cache/flush` | drop the in-memory resolved-value cache (rotation) |

## 12. n8n mirror synergy

The existing mirror (`backend/modules/n8n_credentials/`) resolves a `$NAME` and
POSTs it into an n8n instance as a native credential. Because brokered refs resolve
through the same `decrypt_value()`, a `$infisical:STRIPE_KEY` ref becomes mirrorable
with no mirror change. That closes the loop raised earlier: Infisical is the source
of truth, AgeniusDesk resolves it, n8n gets a native credential in its dropdown, and
no secret lands in workflow JSON. Confirm the mirror accepts scheme-routed refs
(it should, it just calls `decrypt_value`); add a test.

## 13. Trust and isolation posture

- `in_process` only, by design (section 3). Documented on the consent screen.
- The module can resolve any `$ref`, same as any in-process code today. Per-secret
  scopes are not a security boundary (they gate only the n8n mirror), so we do not
  pretend the backend enforces them.
- Self-protection analogue: the module must not let a broker misconfiguration make
  the local store unreadable. Explicit-scheme misses return unresolved; they never
  throw into `decrypt_value`.

## 14. Testing plan

- Unit: mock httpx; assert UA login caching, raw-secret URL/qs, scheme routing in
  `_resolve_secret_ref`, explicit-miss returns `$ref` unchanged, fallthrough on/off.
- Registry: `register`/`unregister` behavior, unknown scheme falls to local.
- Integration (manual, self-hosted Infisical on the LAN): connect, test, resolve a
  known key via `$infisical:KEY`, mirror it into a dev n8n instance, confirm the
  native credential is created.
- agent-vault: resolve `$vault:NAME` against the sidecar admin API; separately
  verify Mode B proxy egress with a Fleet agent (tracked with the Fleet spec).
- Security: assert no resolved value appears in logs; assert `secret_backends.json`
  never contains plaintext.

## 15. Milestones

| # | Deliverable | Est |
|---|---|---|
| M1 | Host registry in `config.py` + `_resolve_secret_ref` hook + tests | 0.5 day |
| M2 | Module scaffold, connections store, `infisical` secret template | 0.5 day |
| M3 | Infisical backend: UA login, resolve, test/connect endpoints | 1 day |
| M4 | Frontend connect + browse view | 0.5 day |
| M5 | agent-vault Mode A resolver | 0.5 day |
| M6 | n8n mirror confirmation + consent-screen trust wiring | 0.5 day |
| M7 | agent-vault Mode B proxy egress (coordinate with Fleet) | 1 day |

Infisical value-resolution path (M1 to M4, M6) is a shippable v1 in about 3 days.
agent-vault Mode B is the stretch that pairs with the Fleet.

## 16. Open questions

1. Short-form `$infisical:KEY` uses the `default` connection. If an operator adds
   two Infisical connections, do we require a per-connection scheme alias
   (`$infisical_prod:KEY`) or keep one default and force qualified form for the
   rest? Leaning: one default, qualified form for the second.
2. Fallthrough default. Spec says off. Revisit if operators find explicit schemes
   annoying in practice.
3. agent-vault admin API shape: confirm how close it is to Infisical's so the
   client code can actually be shared vs needing a separate adapter.
4. Cache TTL default (300s) vs rotation responsiveness. Flush endpoint covers the
   urgent case.
```
