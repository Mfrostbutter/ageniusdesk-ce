# Spec: Fleet Health contribution API

Status: CORE LANDED (2026-07-01, branch `feat/fleet-health-contribution-api`).

**Shipped:** the `health/` module promoted from placeholder to aggregator —
`registry.py` (in-process providers + isolated pull sources), `aggregator.py`
(bounded fan-out, per-source timeout + short-TTL cache, untrusted-row
validation/caps, degraded-not-fatal), `GET /api/health/fleet` merging the n8n
roll-up with contributed `sources`+`summary` (backward-compatible: `instances`+
`totals` untouched). `fleet_health` manifest decl; the loader auto-registers a pull
source for isolated community modules with the decl and drops it on uninstall.
Frontend Sources section on the Fleet Health view. 14 unit tests + end-to-end route
check; full suite green (283).

**Deferred (honest):** pulling in_process community modules (isolated tier only for
now — an in_process module renders in its own view until run isolated); operator
show/hide + reordering of sources; historical/trend storage (rows are point-in-time);
per-source auth beyond the worker proxy secret; the frontend `detail_url` deep-link
wiring (the API validates + returns it; the card link is cosmetic pending router
integration). Consumer wiring (Proxmox manifest `fleet_health` decl) is a one-line
follow-up on that module's branch.

Date: 2026-07-01

Related: `2026-06-28-community-module-candidates.md` (names this the second host
investment gating the Homelab Pack), `2026-06-30-proxmox-module.md` / `2026-06-30-
support-ticketing-module.md` (both defer their Fleet-Health row to this API),
`backend/modules/n8n_proxy/client.py` (`fleet_health()` + `_instance_health()` — the
roll-up shape + the degraded-not-fatal contract this generalizes),
`backend/modules/health/` (the builtin placeholder this promotes into the
aggregator), `frontend/js/views/fleet-health.js` (the consumer).

## 1. What this is

The one thing every "folds into Fleet Health" claim in the module specs waits on.
Today `fleet_health()` lives in `n8n_proxy`, fans out across configured **n8n
instances**, and returns `{instances:[…], totals:{…}}` — there is **no way for
another module to publish a health row**. So Proxmox, the support inbox, and the
rest of the homelab pack can *build* their health data but cannot render it in the
pane the whole pitch ("one client becomes ten" → "one cluster, N nodes") is built
around.

This API adds a **fleet-source registry + a merged roll-up**: a loaded module
publishes one or more `{label, status, metrics}` rows, and the Fleet Health view
renders them next to the n8n instance cards. It generalizes the existing
`_instance_health` pattern (fan out in parallel, a degraded source is shown not
fatal) from "n8n instances only" to "n8n instances + any contributing module."

## 2. Design principles

1. **Reuse the n8n roll-up contract, don't invent one.** A contributed row mirrors
   `_instance_health`'s shape and its **degraded-not-fatal** rule: a source that is
   slow, unreachable, or throwing comes back as a `down` row with an error string,
   never an exception that breaks the whole roll-up.
2. **Pull, not push (§7).** The host pulls each source at aggregation time over the
   transport that already fronts the module; a source does not push rows into the
   host. This needs **no new bridge namespace** and no staleness cache, and it makes
   an unreachable module render as `down` immediately — exactly like an unreachable
   n8n instance.
3. **Contributed rows are untrusted display data.** A community module supplies
   them; the host validates the schema, caps counts and string lengths, constrains
   `detail_url` to in-app paths, and the frontend escapes on render (§9).
4. **The n8n roll-up stays native + backward-compatible.** `{instances, totals}` is
   unchanged; the API **adds** `sources` + `summary`. Nothing that consumes the
   current shape breaks.
5. **One contract, two contributor kinds.** Core built-ins register an in-process
   provider callable; isolated community modules declare a health route the host
   pulls. Both resolve to the same registry entry.

## 3. Where it lives: promote the `health/` module

`backend/modules/health/` is a **builtin placeholder** ("Endpoint health polling,
not yet implemented") with an empty `__init__.py`. Its name and stated purpose are
exactly this, so it becomes the **Fleet Health aggregator + source registry**,
rather than coupling `n8n_proxy` to arbitrary contributors:

- `n8n_proxy.fleet_health()` stays as-is (the n8n-native roll-up). No reverse
  dependency on other modules.
- The `health` module owns the registry and a **merged endpoint** that calls
  `n8n_proxy.fleet_health()` for the n8n rows AND the registry for contributed rows.

New surface in `health/`:

```
manifest.json    (builtin; add routes_prefix /api/health)
__init__.py      re-export router
registry.py      the FleetSource registry (in-process singleton) + register/unregister
router.py        GET /api/health/fleet  → merged {instances, totals, sources, summary}
aggregator.py    fan-out + per-source timeout + cache + degraded-not-fatal wrapping
```

The existing `GET /api/n8n/fleet/health` remains for API stability; the Fleet Health
view migrates to `GET /api/health/fleet` (the superset).

## 4. The contributed-row schema (the contract)

A source returns **one or more rows**. Each row:

```json
{
  "id": "proxmox:homelab",
  "kind": "proxmox",
  "label": "Proxmox — homelab",
  "reachable": true,
  "status": "ok",
  "error": "",
  "metrics": [
    { "label": "nodes", "value": 3 },
    { "label": "running", "value": 12 },
    { "label": "stopped", "value": 1 }
  ],
  "detail_url": "/modules/proxmox"
}
```

| Field | Req | Meaning + validation |
|---|---|---|
| `id` | yes | Stable unique id within the source (`^[a-z0-9][a-z0-9:_-]{0,63}$`). Namespaced `kind:instance` by convention. |
| `kind` | yes | Category, drives grouping/icon (`^[a-z0-9_-]{1,32}$`). |
| `label` | yes | Display string, plain text, ≤ 80 chars (truncated). |
| `reachable` | yes | Bool. `false` ⇒ `status` forced to `down`. |
| `status` | yes | `ok` \| `degraded` \| `down`. `degraded` = up but unhealthy (e.g. a node over threshold). |
| `error` | no | Short string when not reachable/degraded, ≤ 200 chars. |
| `metrics` | no | Ordered list of `{label, value}`, **≤ 8** per row; `label` ≤ 24 chars, `value` a number or string ≤ 40 chars. Display only — no computation host-side. |
| `detail_url` | no | Deep-link into the contributing module's view. **Must be an in-app absolute path** (`^/[A-Za-z0-9/_-]*$`); external schemes / `javascript:` / `//host` rejected and dropped. |

The host **validates every row and drops malformed ones** (a bad row does not fail
the source; the source just contributes fewer rows). A source may return **≤ 32
rows** (excess truncated with a logged note — no silent cap).

## 5. Registration + discovery (two contributor kinds)

**a) In-process provider (core built-ins).** A core module registers an async
callable at load:

```python
health.registry.register_source(
    id="…", kind="…",
    provider=async_fn,            # () -> list[row]
    ttl=25,                       # optional per-source cache seconds
)
```

`provider` runs in the host process (first-party, trusted). Deregistered on nothing
in v1 (built-ins are permanent).

**b) Pull descriptor (community / isolated modules).** A module declares a health
route in its manifest; the loader auto-registers a pull source that fetches it:

```json
"fleet_health": { "enabled": true, "route": "fleet-health", "label": "Proxmox" }
```

- `route` (default `fleet-health`) is relative to `routes_prefix`; the pull URL is
  `{routes_prefix}/{route}` (e.g. `/api/proxmox/fleet-health`), returning
  `{ "rows": [ …row… ] }`.
- `register_modules` registers/unregisters the pull source as the module is
  loaded / unloaded / uninstalled, so a removed module's rows disappear.
- The pull uses the **same reverse-proxy transport that already fronts `/api/{id}/*`**
  (in_process: the mounted route; subprocess/container: the worker proxy client with
  the proxy secret). **No new auth surface, no new bridge namespace** — the health
  route is just another module route the host can reach.

Both kinds land as one `FleetSource` in the registry; the aggregator treats them
identically.

## 6. Aggregation

`GET /api/health/fleet` (in `health/router.py`):

1. `asyncio.gather` over: `n8n_proxy.fleet_health(exec_limit)` **and** every
   registered source's provider.
2. **Each source call is wrapped** with a per-source **timeout** (e.g. 5s) and a
   try/except that converts any failure/timeout into a single `down` row with the
   error string — the aggregator **never raises** (mirrors `_instance_health`).
3. **Per-source result cache** (default ~25s TTL, `register_source(ttl=…)`
   overridable): rapid Fleet-Health polls do not re-hit every module. A timed-out/
   errored source is **negatively cached** briefly so one hung module can't stall
   successive roll-ups.
4. Merge into:

```json
{
  "instances": [ …n8n rows… ],   // unchanged
  "totals":    { …n8n totals… }, // unchanged
  "sources":   [ …validated contributed rows, flattened… ],
  "summary": {
    "sources_total": 4, "sources_ok": 2,
    "sources_degraded": 1, "sources_down": 1
  }
}
```

`summary` counts contributed sources only (n8n keeps its own `totals`); a future
step could compute a unified health score across both, but v1 keeps them side by
side so nothing about the n8n numbers changes.

**Guidance to contributors:** the health route/provider must be **cheap** — return
the module's own last-known cached state, not a live upstream call. The Proxmox
module already keeps live cluster state for its view; its `fleet-health` route
returns a summary of that, it does not hit the Proxmox API on every host poll.

## 7. Pull vs push (why pull)

A push model (a `fleet.publish` bridge namespace the module calls to post rows) was
considered and rejected:

- **Push** needs a new bridge namespace, a host-side **staleness cache** (when did
  this module last publish? is it still alive?), and a **background publisher loop**
  inside every contributing module. A module that hangs keeps showing stale-but-ok
  rows until the cache expires.
- **Pull** needs none of that: the host asks at roll-up time over the transport that
  already fronts the module, a dead/slow module is **immediately** a `down` row (the
  exact `_instance_health` behavior), and there is no inverted data flow to secure.

Pull is also the honest-isolation choice: the host never needs the module's Python
and never holds module-pushed state. (Contrast the secret-backend inversion, which
genuinely needs a host-side resolver — this does not.)

## 8. Isolation fit

Because contribution is **pull over the existing proxy**, it works identically
across tiers with no new machinery:

- **in_process** community module: the `fleet-health` route is mounted; the host
  calls it directly.
- **subprocess / container**: the route runs in the worker; the host pulls it via
  the same reverse-proxy client (proxy secret) that carries all `/api/{id}/*`
  traffic. The worker needs no host credential and no bridge call to contribute.

So a fully-sandboxed Proxmox or homelab module contributes its Fleet-Health row with
**zero** added capability surface — a nice property that falls out of choosing pull.

## 9. Security (contributed rows are untrusted)

A community module's rows are attacker-controlled input to a core surface. The host:

- **Validates the schema** (§4) and drops malformed rows; enforces the per-row
  metric cap (≤ 8) and the per-source row cap (≤ 32).
- **Bounds strings** (`label`/`error`/metric values truncated) so a source can't
  bloat the roll-up or the WebSocket payload.
- **Constrains `detail_url`** to an in-app absolute path; rejects external URLs,
  `javascript:`, and `//host` (no open-redirect / script injection via a deep-link).
- **Escapes on render.** `fleet-health.js` already escapes via `esc()`; source cards
  reuse it. No row field is ever inserted as HTML.
- **Isolates failure**: a source that returns garbage, times out, or 500s becomes a
  `down` row; it never degrades the n8n roll-up or another source.

## 10. Frontend

`fleet-health.js` Health tab gains a **Sources** section under the instance cards
(or interleaved, grouped by `kind`), reusing the existing card pattern: a reachable
source shows its `label` + `metrics` grid + an "open ↗" link to `detail_url`; an
unreachable/`down` source shows the red-left-border card with its `error` (identical
to the existing unreachable-instance card). `status: degraded` uses the amber
treatment already used for mid-range error rates. Purely additive to the view.

The Overview stat cards (the configurable error-window cards) MAY consume `summary`
for a "Sources: N (M degraded)" card; that is a small optional follow-on, not v1.

## 11. Consumers (specs already waiting on this)

- **Proxmox** (`2026-06-30-proxmox-module.md` §8) — publishes
  `{label:"Proxmox: <cluster>", metrics:{nodes, running, stopped}}`; built to the
  degraded-not-fatal contract like `_instance_health`.
- **Support / ticketing** (`2026-06-30-support-ticketing-module.md` §11) — publishes
  `{label:"Support", metrics:{open, urgent}}`.
- **Homelab pack** — TrueNAS (pool/disk health), Uptime Kuma (monitors up/down),
  reverse proxy (cert expiry), Tailscale/NetBird (tunnel up/down), Pi-hole
  (blocking on/off). Each is one source, most just a cheap summary of state the
  module already holds for its own view.

Each of these currently renders its count only on its own nav badge / an Overview
card; this API is what moves them into the shared pane.

## 12. Phasing

- **v1:** the `health/` promotion — registry (both contributor kinds), the manifest
  `fleet_health` decl + loader discovery, the merged `GET /api/health/fleet`
  (fan-out + per-source timeout + cache + degraded-not-fatal), row validation +
  security caps, the frontend Sources section. Migrate Fleet Health view to the
  merged endpoint.
- **Deferred:** a unified health score across n8n + sources; Overview `summary` card;
  a push/streaming variant (only if a source genuinely needs sub-poll latency —
  none identified); per-source history/trend (the roll-up is point-in-time in v1).

## 13. Open questions

- **n8n as a generic source?** v1 keeps n8n native (`instances`/`totals`) for
  backward compat. Refactoring n8n into just another registered source would unify
  the model but is a breaking change to the current shape — worth it, later?
- **Push transport for a future streaming source.** If any homelab source ever needs
  live (sub-poll) status, does it get a `fleet.publish` bridge method then, or ride
  the deferred streaming bridge? (No consumer needs it today.)
- **Per-source auth/role.** Every operator sees every source in v1 (matching the
  current all-instances Fleet Health). Per-client/per-source visibility is the
  enterprise multi-tenancy concern, not this.
- **Cache TTL default + override UX.** 25s is a guess; expose per-source `ttl` only,
  or a global Fleet-Health refresh setting too?

## 14. Testing

- **Registry:** in-process source registers and its rows appear in the roll-up; a
  pull source is discovered from a manifest `fleet_health` decl and unregistered on
  uninstall (rows disappear).
- **Aggregation:** n8n `instances`/`totals` unchanged and present; contributed
  `sources` + `summary` added; a source that raises / times out becomes exactly one
  `down` row and the rest of the roll-up is intact (degraded-not-fatal); the whole
  endpoint never 500s on a bad source.
- **Cache:** two rapid calls hit the source once (TTL); a negatively-cached errored
  source is not re-hit within the window.
- **Validation/security:** a malformed row is dropped (source still contributes its
  valid rows); > 32 rows truncated (with a log note); an over-long `label`/metric
  truncated; a `detail_url` of `https://evil`, `javascript:…`, or `//host` is
  rejected/blanked while `/modules/proxmox` passes; rendered output is escaped.
- **Isolation:** a subprocess and a container module's `fleet-health` route is pulled
  over the proxy transport with no bridge call and no host credential in the worker;
  identical rows in_process and isolated.
- `uv run pytest`; lint touched files `uvx ruff check` (line-length 120).
