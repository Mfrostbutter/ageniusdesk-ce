# Proxmox community module v3: CE host support

Status: delivered in CE 0.6.0 (2026-09-05); see `2026-09-05-host-http-bridge-build.md`
for the build. The v3.1 follow-ups below remain open.

Date: 2026-09-05

Canonical feature spec: [Proxmox community module v3](https://github.com/Mfrostbutter/ageniusdesk-community-modules/blob/main/modules/proxmox/SPEC-v3.md).

## Why this host note exists

The Proxmox community module is intentionally credentialless when isolated. It
names a declared endpoint and sends only a relative path; AgeniusDesk owns the base
URL, token, TLS decision, and network request.

The inspected CE `main` branch at version 0.5.0 contains the design document for
`http.request`, but not its runtime implementation:

- `HostBridgeCapability` currently models `assistant` and `broadcast`, not `http`;
- Pydantic therefore ignores the Proxmox manifest's `host.http` block;
- `bridge.mint()` does not create HTTP endpoint grants;
- `/api/_host/http/request` is not registered;
- the community module's isolated `_host.py` already calls that missing route.

The module works through its in-process compatibility path, but subprocess and
container isolation are not release-ready. Inventory v3 should not deepen that
dependency until the host contract is implemented.

The compatibility path currently reads the host secret store and opens the
Proxmox connection itself. Replace it with a call to the same host-owned policy and
request implementation used by the bridge. One policy implementation must govern
both in-process and isolated modules.

## Required before Proxmox v3 Phase 1

### 1. Implement the existing `http.request` spec

Build `docs/specs/2026-06-28-http-request-bridge.md` and its tests. The bridge must
model endpoints in `backend/module_registry.py`, mint grants from validated models,
resolve secrets at call time, enforce the endpoint/method/path/host/TLS policy, pin
resolved IPs, reject redirects, cap responses, and return the documented wire
shape.

The Proxmox module is the acceptance fixture. Its existing cluster reads and one
guarded power operation must pass unchanged in all three isolation modes.

After migration, the Proxmox manifest should declare `host.http` without the
current unrestricted `network.enabled=true, hosts=[]` fallback. The scanner should
recognize the host facade as a declared bridge call, not direct external egress.

### 2. Add trusted worker identity

Current isolated proxying strips browser identity and the Proxmox worker defaults
its actor to `operator`. That is insufficient for RBAC and reliable audit.

Required contract:

1. Host authentication and CSRF checks run before the module proxy.
2. The proxy rejects/strips inbound `X-AGD-User`, `X-AGD-User-Id`, and
   `X-AGD-Role`.
3. Host-side route policy authorizes the request before forwarding it.
4. The proxy injects trusted user id, display name, and role headers for the worker.
5. The worker uses those headers only for audit and defense-in-depth checks.

Minimum policy for Proxmox v3:

| Route family | Minimum role |
|---|---|
| Cluster/inventory/health reads | viewer |
| Export | viewer |
| Start/cancel deep collection | operator |
| Settings | operator |
| Power/provision/delete | operator |

The host policy must be derived from manifest-declared route classes or another
host-owned configuration. Do not authorize based only on worker code.

### 3. Support a real read-only install grant

The installer already distinguishes read from mutating endpoint methods in the
design. Let the operator reduce a module endpoint's effective methods to
`GET/HEAD`, even when the manifest requests POST/DELETE. Persist that reduction in
the effective endpoint revision and mint the worker grant from the reduced set.

The Proxmox frontend can then hide management controls based on a host-reported
grant summary. This is stronger than the module-local read-only setting because a
compromised worker cannot bypass it.

### 4. Verify binary response proxying

The existing isolated reverse proxy streams response bodies and forwards safe
headers, which should support XLSX/ZIP exports. Add regression tests for:

- `Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`;
- `Content-Disposition: attachment` with a sanitized filename;
- a multi-megabyte streaming body;
- upstream stream closure on success, cancellation, and client disconnect;
- no forwarding of `Set-Cookie`, `Clear-Site-Data`, or authentication headers.

No new host file-write API is needed. The module can stream a spooled workbook.

## Follow-up for Proxmox v3.1

### Repeatable endpoint instances

Multi-cluster support should be a reusable host feature, not two hardcoded Proxmox
manifest entries. Add endpoint templates with operator-created named instances.
Each instance has its own effective base URL, token reference, TLS policy, pinned
IP set, method subset, and consent revision. Workers receive opaque instance ids
and display names only.

Required host operations:

- list endpoint instances granted to the module;
- create/update/delete an instance through the authenticated module manager;
- re-consent and re-pin when host/TLS/methods change;
- atomically refresh an isolated worker grant after config change;
- identify one default instance for backward-compatible unscoped routes.

### Community module scheduler

Do not let workers create independent perpetual scheduler loops. A later host API
should register a bounded interval job, invoke an authenticated module route, and
record last/next run plus result. Scheduled artifact destinations should use the
existing vault/backup abstractions and their capability checks.

## CE acceptance criteria

1. Unknown manifest `host` fields fail validation instead of being silently
   ignored.
2. A Proxmox token never appears in worker env, request input, response, logs, or
   audit.
3. An isolated worker cannot change endpoint host, scheme, port, credential, TLS
   policy, or allowed method.
4. A read-only grant rejects Proxmox POST/DELETE at the host bridge even if the
   worker calls it directly.
5. A browser cannot spoof the worker actor/role headers.
6. In-process, subprocess, and container transports return equivalent upstream
   status/body/truncation semantics.
7. XLSX and ZIP downloads work through both worker proxy implementations.
8. Current community-module tests remain green, plus a real bridge contract test
   replaces the present monkeypatched isolated transport coverage.
9. The module no longer imports the host secret store or makes a direct Proxmox
   request in in-process mode.

## Delivery order

1. Merge the existing `http.request` host bridge.
2. Merge trusted worker identity and host-side route policy.
3. Merge read-only endpoint grant reduction and binary regression tests.
4. Release CE and lock the new app version.
5. Set the Proxmox module's `min_app_version` to that release.
6. Begin Proxmox v3 Phase 1.
