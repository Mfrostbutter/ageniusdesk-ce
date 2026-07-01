# Spec: Proxmox module (plug-and-play infra control)

Status: SPEC / proposed. Not committed to a release.

Date: 2026-06-30

Related: `2026-06-28-integration-modules-roadmap.md` §2 (the committed intent),
`2026-06-28-community-module-candidates.md` (Homelab Pack v1 core; the
credential-under-isolation gap), `2026-06-28-http-request-bridge.md` (the host
capability this module is the flagship consumer of — Proxmox is its worked
example), `2026-06-27-out-of-process-backend-isolation.md` (tiers + proxy + consent
pipeline), `backend/modules/docker_mgr/client.py` (the self-protection pattern this
generalizes: `self_container`/`is_self_container`), `backend/modules/n8n_proxy/
client.py` (`fleet_health()` + `_instance_health()` — the roll-up shape).

## 1. What this is

A **community module** (opt-in, installed via the GitHub inspect / scan / consent
pipeline) that connects to a Proxmox VE cluster and gives the operator a read-first
control surface over it: list nodes, VMs, and LXCs with live status; node + cluster
health; and gated start / stop / reboot on guests. Onboarding *feels* like adding an
n8n instance — point it at the management console URL, give it an API token, test on
save — so "one client becomes ten" extends to "one cluster, N nodes." (The one
difference the operator will notice: the bridge model is **one cluster per install**,
§4 — you install the module again for a second cluster, you don't add it in-app.)

Proxmox is the **flagship consumer of the `http.request` bridge.** It is the bridge
spec's own worked example (the `PVEAPIToken={value}` header injection, `verify_tls:
false` for self-signed LAN). Once the bridge ships, Proxmox is "just another REST
module": the cluster token stays host-side, the worker orchestrates, the host makes
the authenticated call. Until the bridge ships, a credential-holding Proxmox module
runs `in_process`-only (full host access) — so **the bridge is a hard dependency
for the isolated build** (§3).

## 2. The dependency and sequencing

Proxmox holds a cluster credential, so it sits squarely in the candidates doc's
"credential-holding REST quadrant." Its isolation posture is entirely determined by
the `http.request` bridge:

| Bridge state | Proxmox posture |
|---|---|
| Bridge shipped | **Safe under container/subprocess isolation.** Token host-side; module calls `host.http.request({endpoint: "proxmox", …})`; identical code across tiers (the `assistant.complete` dual-mode pattern). |
| Bridge not shipped | **`in_process`-only.** Module reads the token directly and calls Proxmox itself — full host access, no containment. Acceptable only as a stopgap. |

**Sequencing (locked by the candidates doc):** build the `http.request` bridge
first; Proxmox is then not a special case. This spec assumes the bridge exists and
specs Proxmox as a bridge consumer. If Proxmox must ship before the bridge, it ships
`in_process` with a documented "runs with host access until the http bridge lands"
note — not the intended end state.

## 3. Auth: API token is the only clean isolated auth

Proxmox offers two auth mechanisms; only one fits the bridge, and this is a
load-bearing buildability finding:

- **API token (v1 default).** A Proxmox API token is a **static header**:
  `Authorization: PVEAPIToken=USER@REALM!TOKENID=SECRET`. That maps exactly onto the
  bridge's `header` auth type with `format: "PVEAPIToken={value}"` (the bridge spec's
  literal Proxmox example). The `{value}` is the **entire**
  `user@realm!tokenid=secret` string — the operator stores that whole string as the
  one `PROXMOX_TOKEN` secret (not just the UUID secret half, a common mistake that
  yields 401s; the connect form says so). The host injects it per call. Clean,
  stateless, rotation-friendly.
- **Username / password realm login (deferred / `in_process`-only).** Ticket auth is
  a **stateful two-step**: `POST /access/ticket` returns a `PVEAuthCookie` (2-hour
  TTL) **plus** a `CSRFPreventionToken` that every subsequent mutating call must
  echo in a header. This does **not** fit the bridge: the bridge injects a single
  static auth header, strips `Cookie` from worker-set headers, and holds no session
  state. Supporting it would need a stateful, cookie-carrying, CSRF-tracking bridge
  mode — out of scope. So v1 under isolation is **API-token only**; realm login is
  either `in_process`-only or a later bridge enhancement.

**Decision:** v1 requires an API token. The connect form asks for the console URL +
an API token id + secret, and says plainly that a token is required (with a one-line
"how to create a Proxmox API token" pointer). This is stricter than the roadmap's
"API token preferred, user/realm fallback," and the reason (bridge fit) is why.

Token scope guidance surfaced at connect: recommend a **least-privilege token**
(a dedicated role/token, `PVEAuditor` for read-only installs; a limited role with
`VM.PowerMgmt` for the action set) rather than root@pam. The module cannot enforce
this — Proxmox does — but it should tell the operator.

## 4. The endpoint model (and where it strains "add like an n8n instance")

The bridge's endpoints are **declared in the manifest** (a fixed set), with the
effective `base_url` operator-overridable per install (bridge spec §3). Proxmox
declares one endpoint:

```json
"host": {
  "http": {
    "enabled": true,
    "endpoints": [
      {
        "id": "proxmox",
        "base_url": "https://your-proxmox:8006/api2/json",
        "auth": {
          "type": "header", "header": "Authorization",
          "secret_ref": "PROXMOX_TOKEN", "format": "PVEAPIToken={value}"
        },
        "methods": ["GET", "POST"],
        "verify_tls": false
      }
    ]
  }
}
```

`methods` includes `POST` because start/stop/reboot are POSTs — a **mutating
endpoint**, so it triggers the bridge's stronger per-endpoint consent (§7).
`verify_tls: false` is the homelab default for Proxmox's self-signed cert;
the scanner surfaces it as an INFO note, not hidden.

**The strain (honest finding).** The roadmap wants Proxmox added "like an n8n
instance" — dynamically, many at runtime. The bridge's endpoint set is
**manifest-declared** with per-install `base_url` override, i.e. effectively **one
cluster per install**. n8n instances are runtime-dynamic (`get_instances()`); the
bridge model is not. So:

- **v1: one cluster per install.** The operator sets the single `proxmox` endpoint's
  `base_url` + token at install. Managing a second cluster = a second install of the
  module. This matches the bridge model with zero new host work.
- **Multi-cluster (open, §11):** operator-added-at-runtime endpoints need the bridge
  to support an **operator-managed endpoint list** (its "effective config is
  host-side per install" already points that way — extend it from a fixed list to an
  operator-appendable one, each new entry re-running endpoint consent). That is a
  bridge enhancement, tracked there, not built here.

This is the one place the "add like an n8n instance" vision and the bridge's
security model diverge; v1 takes the secure, one-per-install path and names the gap.

## 5. Scope (the API surface)

All calls go through `host.http.request` to the `proxmox` endpoint. Read set (the
whole default experience) uses `GET`; the action set uses `POST`.

**Read (GET, the default):**
- `GET /cluster/resources?type=vm` — one-shot roll-up of every guest (VM + LXC) with
  its node, status, name, cpu/mem/uptime. The primary fleet view; one call paints
  the whole cluster.
- `GET /cluster/status` — quorum + node membership (cluster health).
- `GET /nodes` and `GET /nodes/{node}/status` — per-node health (cpu, memory,
  uptime, load).
- `GET /nodes/{node}/qemu` / `.../lxc` — per-node guest detail when drilling in.

**Actions (POST — every action in this module is a POST; there is no PUT/PATCH/DELETE
surface in v1, gated — §6/§7):**
- Guest power: `POST /nodes/{node}/qemu/{vmid}/status/{start|shutdown|stop|reboot}`
  and the `lxc` equivalents. `shutdown` = graceful (guest OS), `stop` = hard pull,
  `reboot` = restart. **UI labeling matters:** PVE `stop` is a hard power pull, so the
  control is labeled **"Force stop (hard)"** (distinct from "Shutdown"), since "Stop"
  reads as graceful in most UIs and here it is not.
- Node power (`POST /nodes/{node}/status` `{command: reboot|shutdown}`) is
  **hard-gated** (§6): it affects every guest on the node. v1 may omit node power
  entirely and document it as a console action.

Everything else in the Proxmox API (create/clone/delete guests, storage, backups,
firewall) is **out of v1** — read + power-cycle is the plug-and-play surface. Backups
have their own committed roadmap item (scheduled backups); this module does not
duplicate it.

## 6. Self-protection (never nuke the box you run on)

This module generalizes the Docker self-protection pattern
(`docker_mgr.is_self_container` → refuse destructive actions on the dashboard's own
container). Proxmox makes it sharper: AgeniusDesk very commonly **runs as an LXC or
VM on the cluster it manages**, so a stop/reboot on the wrong guest — or a shutdown
of the node hosting it — takes the dashboard down from inside the dashboard.

The hard problem: unlike Docker (procfs container id), **Proxmox has no reliable
self-detection.** A guest does not know its own vmid or host node from inside. So:

- **Operator-declared self-guest (v1).** At connect, an optional "This dashboard
  runs on this cluster" step captures `{node, vmid, type}`. When set, the module:
  - **refuses** `stop`/`shutdown`/`reboot` on that guest (403 + a clear "this is the
    dashboard's own guest; power-cycle it from the Proxmox console"),
  - **refuses** node shutdown/reboot on the node hosting it,
  - **marks** that guest and node in the UI and **hides their destructive controls**
    (exactly the `is_self` flag + hidden-controls treatment `docker_mgr` uses).
- **The guard is enforced server-side, not by the UI.** `guard.is_self_guest()` is
  checked in `router.py` **before** the bridge POST is issued; the UI hiding the
  controls is defense-in-depth, not the boundary. This matters because the iframe is
  same-origin with the host proxy, so a crafted POST from the iframe (or any
  authenticated caller) could reach the action route — the server-side check is the
  real gate, which is why §12's dogfood asserts a *direct* API attempt 403s, not just
  that the button is hidden.
- **Where the self-guest declaration + read-only toggle persist.** In a
  **module-private settings store** — a `settings` table in the same module-owned
  `AGD_MODULE_DATA_DIR` DB as the audit log (§9), keyed per install:
  `{self_node, self_vmid, self_type, read_only}`. **Not** the bridge endpoint config
  (the bridge knows only `base_url`/`secret_ref`/pins — it has no concept of "which
  guest is the dashboard"). The guard reads this store on every action.
- **Best-effort auto-hint (false positives are acceptable).** The module MAY match the
  dashboard's own IP/hostname against `cluster/resources` guest network info to
  *suggest* the self-guest at connect. Because it is only ever a **suggestion the
  operator confirms** — the UI never pre-fills or auto-applies the declaration — a
  wrong guess is harmless (the operator declines it). So a loose match with false
  positives is fine; the invariant is "suggest, never set."
- **Read-only mode toggle.** A per-install switch (persisted in the settings store
  above) that disables the entire action set (the guard rejects all actions
  server-side; the endpoint stays effectively GET/HEAD). The cautious operator runs
  read-only; the toggle is independent of self-protection and stacks with it.
- **Node-power caution.** Any node shutdown/reboot (if offered at all) carries an
  extra warning that it affects all guests on the node, and quorum implications for
  a clustered setup.

Destructive actions are additionally gated at the **operator role + an explicit
confirm** (the host already enforces role + CSRF at the proxy before forwarding
`/api/{id}/*`; the UI adds the typed/confirm step, mirroring the Docker destroy
flow).

## 7. Consent + scanner posture

- Declares `host.http` with the single `proxmox` endpoint. Holds no `worker_secrets`
  (the token is host-side in the bridge, never injected into the worker) → no
  consented-secret finding.
- The `proxmox` endpoint requests `GET` + `POST` only — **no `PUT`/`PATCH`/`DELETE`**.
  So the bridge's **mutating-endpoint consent** (bridge spec §6) covers exactly the
  POST power-cycle actions (start/shutdown/stop/reboot) and nothing else; there is no
  delete-class surface to consent to in v1. The consent block reads "this module can
  **power-cycle** VMs and containers on `your-proxmox:8006`." Read-only installs
  (operator declines the action set / runs read-only mode) get the baseline
  GET-only consent.
- `verify_tls: false` → INFO note in the scan report (visible, not hidden), the
  expected homelab posture.
- No `host.assistant` in v1 (no LLM need). An optional "explain this cluster alert"
  helper via `assistant.complete` is a later add, not v1.

## 8. Fold into Fleet Health (deferred, same gate as everything)

The roadmap wants the cluster roll-up in **Fleet Health** ("one cluster, N nodes"),
matching the n8n `fleet_health()` roll-up shape (`{instances:[…], totals:{…}}`,
per-node `reachable`/counts, degraded-not-fatal). That contribution path is now
specced (`2026-07-01-fleet-health-contribution-api.md`): Proxmox declares a
`fleet_health` manifest route the host pulls, returning `{rows:[…]}`. Until that API
ships:

- **v1:** the module renders its **own** roll-up in its own view (nodes reachable,
  guests running/stopped, per-node cpu/mem) and surfaces a count on its nav badge +
  optionally an Overview stat card. Self-contained.
- **Via the contribution API** (`2026-07-01-fleet-health-contribution-api.md`): the
  module declares `"fleet_health": {route: "fleet-health"}` and serves
  `GET /api/proxmox/fleet-health` → `{rows: [{label:"Proxmox: <cluster>", status,
  metrics:[{label:"nodes",…},{label:"running",…},{label:"stopped",…}]}]}`, returning
  its **cached** cluster state (the route is cheap; the host pulls it, per that
  spec's contributor guidance), degraded-not-fatal. The v1 module can ship the route
  from day one — the host just won't pull it until the API lands, so no stub is
  wasted.

## 9. Module layout (`modules/proxmox/`)

```
manifest.json    id proxmox; routes_prefix /api/proxmox; capabilities:
                 host.http endpoint "proxmox" (GET+POST, verify_tls false),
                 network egress. NO host.assistant, NO worker_secrets in v1.
__init__.py      re-exports router
router.py        APIRouter(prefix="/api/proxmox"); GET cluster/nodes/guests;
                 POST guest power (self-protection + role gate + confirm);
                 GET/POST self-guest declaration + read-only toggle
client.py        thin Proxmox API wrapper over _host.http_request; parses
                 /cluster/resources etc. into the UI shape; NEVER raises on a
                 degraded node (reachable=False + error string, mirrors
                 _instance_health)
guard.py         self-protection: read self-guest + read-only from the settings
                 store; is_self_guest(); refuse destructive actions server-side
                 (mirrors docker is_self_container) — called by router.py pre-POST
service.py       module-private state.db: a `settings` table (self-guest declaration
                 + read_only) and an `audit` table (power actions incl. refusals)
_host.py         dual-mode facade: http_request via bridge (isolated) or a direct
                 host httpx call resolving the secret (in_process)
static/
  proxmox.html   cluster view (nodes + guests, status, per-guest actions), self-guest
                 marked with hidden destructive controls, read-only banner
  proxmox.js     polls AgeniusDesk.fetch; renders cluster; confirm dialogs on actions
README.md  tests/
```

Persisted state is minimal: cluster/guest state is **fetched live**, not stored (like
`fleet_health`). Module-private storage is one `AGD_MODULE_DATA_DIR/state.db` with:

- a **`settings`** table — the self-guest declaration (`self_node`, `self_vmid`,
  `self_type`) and the `read_only` flag (§6). The guard reads this on every action.
- an **`audit`** table — one row per power action (timestamp, actor role, node, vmid,
  action, **result**). **Refused attempts are audited too**: a self-guest 403, a
  read-only rejection, or an auditor-token 403 writes a row with `result=refused` +
  the reason. "Operator X attempted stop on self-guest 108, refused" is exactly the
  governance seed §10 claims, so refusals must be captured, not just successes.

**Connection config** (base_url + token `secret_ref`) lives host-side in the bridge's
per-install endpoint config, not in the module. The self-guest declaration is
**module state** (the bridge has no concept of it) and lives in `settings` above —
these are two different stores and the spec keeps them distinct on purpose.

## 10. Frontend + CE vs Enterprise

- **Frontend:** the sandboxed-iframe **polling** pattern (`AgeniusDesk.fetch`,
  buffered, no push), same as the other community modules. Poll the cluster view on a
  **~5s default** cadence while open (configurable); each poll is **one**
  `/cluster/resources` call that rolls up the whole cluster, not one call per guest,
  so it stays cheap even on a modest homelab PVE with several operators viewing. A
  power action posts, then the next poll reflects the new status.
- **Auditor-token graceful degrade (react-to-first-403).** The module does not probe
  privileges on connect; it reacts to the **first `403` on an action** by flipping the
  view to read-only for that install (disabling the action buttons) and surfacing
  "this token lacks power-management rights." Simpler than a probe call and needs no
  extra API round-trip.
- **CE vs Enterprise:** this module is the **CE building block** — connect, view,
  power-cycle, self-protect. Multi-tenant governance (per-client RBAC on which
  operator may touch which cluster, immutable audit/receipts, approval workflows)
  stays on the separate **enterprise** roadmap. The v1 audit log is the honest seed
  of that, not the governance layer itself.

## 11. Open questions

- **Multi-cluster / dynamic endpoints (§4).** v1 is one cluster per install; the
  runtime-added-endpoint model is a bridge enhancement (operator-managed endpoint
  list + per-endpoint consent). Decide whether that lands in the bridge before or
  after Proxmox v1.
- **Realm (user/password) login.** Deferred under isolation (§3). Is it worth an
  `in_process`-only path for operators without API tokens, or do we hold the line on
  tokens-only?
- **Node power actions.** Include node reboot/shutdown at all in v1, or document it
  as console-only? (Leaning console-only for v1 — high blast radius, low frequency.)

Resolved in this pass (were open): self-guest auto-hint false positives are
acceptable because it only ever suggests (§6); auditor-token degrade is
react-to-first-403, no probe (§10); self-guest + read-only persist in the module's
`state.db` settings table (§6/§9); the guard is enforced server-side (§6).

## 12. Build order

Assumes the `http.request` bridge has landed (§2).

1. Scaffold `modules/proxmox/` (manifest with the `proxmox` endpoint, `__init__`,
   `_host` dual-mode facade from `youtube-research`/the bridge SDK).
2. `client.py` read path: `/cluster/resources` + `/cluster/status` + `/nodes/*` →
   the UI roll-up shape; degraded-node handling mirroring `_instance_health` (never
   raise). Prove a real cluster renders.
3. `state.db` (settings + audit tables) + self-guest declaration + `guard.py`
   (`is_self_guest`, refuse destructive on self-guest/host-node) enforced
   **server-side in `router.py` before the bridge POST** + the read-only toggle.
4. Action path: guest start/shutdown/stop/reboot through the bridge POST, behind the
   server-side guard + role gate + confirm; write an audit row on **every** attempt,
   success or refusal (`result` ∈ ok/failed/refused + reason).
5. Frontend: polling cluster view, self-guest marked with hidden destructive
   controls, read-only banner, confirm dialogs; nav badge + optional Overview card.
6. Tests (§13).
7. Dogfood on a real cluster: connect with an API token, view nodes/guests, mark the
   dashboard's own guest, confirm its power controls are hidden and a direct API
   attempt on it 403s, power-cycle a *different* test guest, read the audit log.
8. **Follow-ups (separate slices):** Fleet-Health row (when the contribution API
   exists); multi-cluster (when the bridge supports operator-added endpoints);
   optional `assistant.complete` alert explainer.

## 13. Testing

- **Auth mapping:** the `PVEAPIToken={value}` header is built host-side and the
  worker never sees the token; a call echo/response never contains it (bridge
  contract, asserted at the module boundary).
- **Read path:** `/cluster/resources` parses into the roll-up; an unreachable node
  yields `reachable=False` + error string, not an exception (mirror
  `_instance_health`); totals aggregate.
- **Self-protection (server-side is the boundary):** with a declared self-guest, a
  **direct** `POST` to `stop`/`shutdown`/`reboot` on it → 403 (not merely a hidden
  button); node shutdown on its host node → 403; the guest/node carry the `is_self`
  flag and the frontend hides their destructive controls; a *different* guest's
  actions succeed. Read-only mode makes the server-side guard reject the whole action
  set even for a direct POST.
- **State store:** the self-guest declaration + read-only flag persist in `state.db`
  `settings` and survive a worker restart; the guard reads them per action.
- **Consent/scanner:** the `proxmox` endpoint declares `GET`+`POST` only (no
  PUT/PATCH/DELETE) and its `POST` triggers the mutating-endpoint consent covering the
  power actions; `verify_tls:false` surfaces as INFO; no `worker_secrets` declared →
  no consented-secret finding.
- **Graceful degrade:** an auditor-only token returning 403 on an action flips the
  install to read-only on the first 403 (no probe, no dead buttons, no crash).
- **Audit incl. refusals:** each power action writes one audit row; a **refused**
  attempt (self-guest / read-only / auditor-403) writes a row with `result=refused` +
  reason, not just successes.
- **Dual-mode:** the same call path runs `in_process` (direct httpx, secret resolved
  host-side) and isolated (via the bridge) with identical results.
- `uv run pytest`; lint touched files `uvx ruff check` (line-length 120).
