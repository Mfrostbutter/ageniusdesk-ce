# Build spec: `http.request` host bridge, trusted worker identity, read-only grants

Status: BUILD. Implements the design in `2026-06-28-http-request-bridge.md` and
the Phase 0 requirements in `2026-09-05-proxmox-community-module-v3-host-support.md`.

Date: 2026-09-05

Related code: `backend/module_registry.py`, `backend/modules/_runtime/{bridge,
endpoints,http_bridge,identity,proxy}.py`, `backend/modules/modules/{installer,
router,scanner}.py`, `frontend/js/views/settings-modules.js`.

## 1. Scope

Four deliverables, in the order the companion document asks for them:

1. The `host.http` manifest model, the effective endpoint store, and the
   `POST /api/_host/http/request` bridge route, with one policy implementation
   shared by isolated workers and in-process modules.
2. Trusted worker identity and host-side route policy for community routes.
3. Operator read-only reduction of an endpoint's effective methods, surfaced to
   the module as a grant summary.
4. Binary response proxying regression tests.

Out of scope (v3.1 follow-ups in the companion): repeatable endpoint instances,
a community-module scheduler, streaming responses, per-module rate limits.

## 2. Manifest model

`capabilities.host` gains `http`. Every model under `host` is `extra="forbid"`, so
an unknown field fails manifest validation instead of being silently dropped.

```json
"host": {
  "http": {
    "enabled": true,
    "endpoints": [
      {
        "id": "proxmox",
        "base_url": "https://your-proxmox:8006/api2/json",
        "auth": {
          "type": "header",
          "header": "Authorization",
          "secret_ref": "PROXMOX_TOKEN",
          "format": "PVEAPIToken={value}"
        },
        "methods": ["GET", "POST", "DELETE"],
        "verify_tls": false
      }
    ]
  }
}
```

Validation rules:

- `id` is a slug (`[a-z0-9][a-z0-9_-]{0,31}`), unique within the module.
- `base_url` is `http` or `https`, has a host, and carries no userinfo, query,
  or fragment. A trailing slash is stripped.
- `methods` are upper-cased and limited to `GET HEAD POST PUT PATCH DELETE`.
  Default `["GET", "HEAD"]`.
- `auth.type` is one of `bearer`, `header`, `basic`, `query`. `secret_ref` is a
  secret name, never a value. `basic` may name `user` (literal) or `user_ref`.

A manifest may also declare route classes so the host can authorize before it
forwards a request to the worker:

```json
"routes": [
  { "pattern": "/api/proxmox/settings", "methods": ["POST"], "min_role": "operator" }
]
```

The host default is `viewer` for `GET`, `HEAD`, and `OPTIONS` and `operator` for
every other method. A declared route may only raise the floor. A module cannot
lower a mutating route to `viewer` from its manifest, and it cannot authorize
from worker code at all.

## 3. Effective endpoint store

`data/module-endpoints.json` holds one revision per `(module_id, endpoint_id)`:

```json
{
  "proxmox": {
    "proxmox": {
      "revision": 2,
      "status": "active",
      "base_url": "https://10.10.0.20:8006/api2/json",
      "declared_methods": ["GET", "POST", "DELETE"],
      "methods": ["GET", "HEAD"],
      "verify_tls": false,
      "auth": { "type": "header", "header": "Authorization",
                "secret_ref": "PROXMOX_TOKEN", "format": "PVEAPIToken={value}" },
      "pinned_host": "10.10.0.20",
      "pinned_ips": ["10.10.0.20"],
      "consented_by": "michael",
      "consented_at": "2026-09-05T18:00:00Z"
    }
  }
}
```

Rules:

- The manifest proposes defaults. The store is the runtime source of truth. The
  bridge never reads `base_url`, `methods`, or `verify_tls` from the manifest at
  call time.
- `status` is `active` or `pending`. A pending revision fails closed with a
  message that points the operator to the module manager. Revisions are seeded
  pending when a module with `host.http` is registered without one (the upgrade
  path for modules installed before this feature).
- `methods` is always a subset of `declared_methods`. The operator may reduce it
  at install or later. `HEAD` is allowed whenever `GET` is.
- Changing `base_url` host, scheme, or port, enabling TLS verification bypass,
  or adding a method starts a new revision and requires explicit consent
  (`consent: true` on the update call). Reducing methods or enabling TLS
  verification does not.
- Pinned IPs are resolved when a revision is created and on explicit re-pin. An
  IP-literal host pins to itself. A hostname that fails to resolve stores an
  empty pin set and the revision fails closed until re-pinned.
- `auth` is copied from the manifest at seed time and is not operator-editable;
  the secret value is resolved per call and never stored.

The bridge grant minted at worker spawn carries only the module id and the set
of declared endpoint ids. Each call looks up the current revision, so a config
change takes effect immediately in every isolation mode without a re-mint.

## 4. Request policy (shared implementation)

`backend/modules/_runtime/http_bridge.py` exposes one coroutine:

```python
await http_bridge.request(module_id, payload, allowed_endpoints=None)
```

The bridge route calls it with the grant's endpoint set. An in-process module
calls it directly with its own module id. Steps, in order:

1. Endpoint id must be in the allowed set and have an `active` revision.
2. Method must be in the revision's effective `methods`.
3. Path validation: reject NUL, backslash, `..` segments, a scheme, a leading
   `//`, `@`, `?`, and `#`. The query goes in `query`, never in `path`.
4. URL = effective `base_url` + `/` + path. The result's scheme, host, and port
   must equal the base's.
5. Worker headers are sanitized: `Authorization`, `Host`, `Cookie`,
   `Content-Length`, hop-by-hop headers, `X-Forwarded-*`, and the endpoint's
   configured auth header are dropped. Injected auth always wins.
6. Auth is resolved from `secret_ref` at call time. A missing secret fails
   closed with HTTP 503 and never sends the reference name as a credential.
7. For a hostname endpoint, the host is re-resolved (60s cache) and must
   intersect the pinned set; the connection dials a pinned IP while TLS verifies
   against the consented hostname. No intersection fails closed with a re-pin
   message.
8. httpx call: `verify=verify_tls`, 30s timeout, redirects disabled.
9. The response body is read up to 5 MB; anything longer sets `truncated`.
10. Response headers are allowlisted: `content-type`, `content-length`,
    `content-disposition`, `etag`, `last-modified`, `retry-after`,
    `cache-control`, `location`.

Wire shape (unchanged from the design spec, plus the binary flag):

```json
{ "status": 200, "headers": { "content-type": "application/json" },
  "body": "...", "truncated": false, "content_encoding": "base64" }
```

`content_encoding` is present only when the body was not valid UTF-8.

Every call is audited as `module.http_request` with module id, endpoint id,
method, path, upstream status, and truncation. Never headers, body, or the
secret.

## 5. Trusted identity and route policy

One middleware (`identity.apply`) runs for every `/api/{community_id}/...`
request in every isolation mode:

1. Inbound `X-AGD-*` headers are removed from the request scope.
2. The host resolves `current_user`. On an open install (login disabled and
   auth not required) the identity is `anonymous` with role `admin`, matching
   how `require_role` treats that mode elsewhere.
3. The route's minimum role is computed from the manifest route classes and the
   host default. An unauthenticated request gets 401; an insufficient role gets
   403. Nothing is forwarded.
4. Trusted headers are injected into the scope: `X-AGD-User`, `X-AGD-User-Id`,
   `X-AGD-Role`, `X-AGD-Auth-Source`. In CE 0.6 the user id is the username.

The reverse proxy forwards those headers to the worker and still strips cookies
and `Authorization`. An in-process router sees the same headers on
`request.headers`. Module code therefore reads identity one way in both modes,
for audit and defense-in-depth only.

## 6. Grant summary

- Bridge: `GET /api/_host/http/endpoints` returns
  `{"endpoints": [{"id", "status", "methods", "verify_tls", "host"}]}` for the
  worker's declared endpoints.
- In-process: `http_bridge.grant_summary(module_id)` returns the same list.
- Operator API: `GET /api/modules/{id}/endpoints` (viewer), `PUT
  /api/modules/{id}/endpoints/{eid}` (operator; body `base_url`, `methods`,
  `verify_tls`, `consent`), `POST /api/modules/{id}/endpoints/{eid}/repin`
  (operator).

The Proxmox module includes the grant in its settings response so the UI can
hide power and provisioning controls when `POST` is not granted.

## 7. Install flow

`POST /api/modules/install` accepts `endpoints: {eid: {base_url, methods,
verify_tls}}`. The consent modal shows every declared endpoint with an editable
base URL, the secret it will use, and a per-endpoint choice between read-only
and the declared method set. Mutating methods are off by default and carry a
separate warning block. Install seeds active revisions from the operator's
choices and pins IPs. Uninstall removes the module's revisions.

## 8. Scanner

- A string containing `/api/_host/http/` without `host.http.enabled` is HIGH
  (`undeclared-host`). Declared use is INFO.
- Importing `backend.modules._runtime.http_bridge` is the in-process facade and
  is reported INFO, not the HIGH host-import finding.
- Each declared endpoint with `verify_tls: false` is an INFO finding so it is
  visible in the report.

## 9. Acceptance tests

Mapped to the companion document's criteria:

| # | Criterion | Test |
|---|---|---|
| 1 | Unknown `host` fields fail validation | `test_http_bridge.py::test_manifest_rejects_unknown_host_field` |
| 2 | Token never in worker env, input, response, logs, audit | bridge tests assert the secret is absent from the echoed response and audit fields; the worker env allowlist tests already cover env |
| 3 | Worker cannot change host, scheme, port, credential, TLS, methods | path pivot, absolute URL, header injection, and manifest-vs-store tests |
| 4 | Read-only grant rejects POST at the bridge | `test_read_only_reduction_blocks_post` |
| 5 | Browser cannot spoof actor headers | `test_worker_identity.py` strips inbound `X-AGD-*` and injects trusted values |
| 6 | Equivalent semantics across transports | the in-process facade and the bridge route share `http_bridge.request`; both are exercised against the same mocked upstream |
| 7 | XLSX and ZIP downloads through the proxy | `test_proxy_binary.py` |
| 8 | Existing tests green plus a real bridge contract test | full suite |
| 9 | Proxmox no longer imports the secret store | Proxmox module `_host.py` rewrite and its tests |

## 10. Version

CE `0.6.0` ships this contract. The Proxmox module sets `min_app_version` to
`0.6.0` and drops its unrestricted `network` declaration.
