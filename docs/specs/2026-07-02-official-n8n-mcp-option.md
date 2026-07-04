# Spec: Official n8n MCP as a peer option (run either, or both, and compare)

Status: Draft
Date: 2026-07-02
Owner: Michael Frostbutter
Scope: AgeniusDesk Community Edition
Release gate: yes (new user-facing integration + Settings surface)
Decision on record: CE keeps czlonkowski's n8n-mcp as the default provider and
adds the official n8n MCP as a first-class second provider. Users can enable
either, or both at once, and a compare view shows the practical difference on
their own instance. We do not replace or deprecate the existing integration.

Phase 0 spike complete (2026-07-02, against live n8n 2.28.5). Confirmed facts are
folded in below and marked "(spike-confirmed)". Net effect on the design: the
official provider CANNOT silently reuse the stored instance API key and CANNOT be
one-click/auto-enabled; it needs a paste-the-token step. The shared-client
`auth_header` change turned out unnecessary and is dropped from v1.

## 1. Goal

CE already ships a built-in n8n MCP: czlonkowski's `n8n-mcp`, provisioned as a
managed container (`backend/modules/assistant/n8n_mcp_provision.py`) and
registered into `config.mcp_servers` with `managed: "n8n-mcp"`. n8n now ships its
own **official** MCP server built into the product. The two are not the same tool
and have real, measurable tradeoffs (see the czlonkowski competitive analysis,
July 2026).

The goal: let a CE operator turn on the official n8n MCP with the same one-click
ergonomics as the existing card, run it beside czlonkowski's, and see a
side-by-side comparison (tool count, validation behaviour, edit-token cost) on
their own connected instance rather than trusting a vendor's benchmark table.

## 2. Key finding (load-bearing; do not re-discover)

The two providers sit at **different layers**, and that is the whole design.

- **czlonkowski `n8n-mcp`** is a standalone server we run. CE pulls
  `ghcr.io/czlonkowski/n8n-mcp:latest`, starts it in `MCP_MODE=http` on a
  published host port, gates it with a minted `AUTH_TOKEN` (Bearer), and probes
  `initialize` + `tools/list` before registering the reachable URL. Node
  intelligence (`docs` mode) needs no n8n credentials; workflow CRUD (`full`
  mode) is wired to the active instance's `N8N_API_URL`/`N8N_API_KEY`. All of
  this is `n8n_mcp_provision.py` today.

- **Official n8n MCP** is served **by the n8n instance itself**, at
  `<n8n_base>/mcp-server/http` (HTTP transport; the n8n docs register it with
  `claude mcp add --transport http n8n-mcp <base>/mcp-server/http`). There is
  **no container for CE to run** — if you have a connected n8n instance new
  enough to expose it, the endpoint already exists. Enabling it in CE is a
  registration + auth problem, not a provisioning problem.
  - (spike-confirmed) The endpoint responds on n8n **2.28.5**:
    `POST /mcp-server/http` returns `401` unauthenticated (not 404), so the
    feature is present in current n8n. Min version still TBD (Open Q2).

This means the official provider reuses almost the entire existing MCP client
path (`backend/modules/assistant/mcp_client.py`: streamable HTTP, session init,
`tools/list`, `tools/call`, SSRF guard via `assert_safe_probe_url`) — including
auth, unchanged:

- (spike-confirmed) Auth is **Bearer**: the 401 carries
  `WWW-Authenticate: Bearer realm="n8n MCP Server"`. Our client already sends
  `Authorization: Bearer <token>` (`mcp_client.py:_headers`), so **no client
  change is needed** and the previously-planned `auth_header` field is dropped
  from v1.
- (spike-confirmed) The bearer is **NOT the n8n REST API key**. n8n mints a
  separate per-user **MCP Access Token**, and an n8n admin must first turn the
  feature on at **Settings -> Instance-level MCP -> Enable MCP access**, then copy
  the token from the Access Token tab. Consequence: CE **cannot** reuse the
  stored instance API key and **cannot** one-click/auto-enable the official
  provider. It needs an explicit paste-the-token enable step (§4.2).

## 3. Why both (the comparison is the feature)

From the competitive analysis, distilled to what a CE operator actually feels:

| Dimension | czlonkowski n8n-mcp | Official n8n MCP |
|---|---|---|
| Where it runs | Container CE manages | Inside n8n; no extra container |
| Works before an instance is connected | Yes (`docs` mode) | No (it *is* the instance) |
| Node/template knowledge | 2,700+ templates, ~1,845 nodes incl. community | Core nodes; no template tools |
| Validation | Errors on broken configs (usable as an agent stop-signal); autofix | Report-only; returned `valid:true` on several broken configs in the analysis |
| Large-field edits | `patchNodeField` find/replace, dry-run | Whole-value edits; gap narrows on n8n v2.20+ |
| Credentials | Full CRUD | Read-only + auto-assign |
| Draft/publish, pin-data testing | Basic active flag | Native (official wins) |

The honest pitch to users: czlonkowski is stronger for autonomous authoring and
trustworthy validation; the official server is the native, zero-extra-moving-parts
option and owns lifecycle features (draft/publish, pin-data). Rather than assert
this, CE lets them prove it on their own workflows.

## 4. Design

### 4.1 Generalize the provider, keep the existing one intact

Refactor the single-purpose `n8n_mcp_provision.py` into a small provider notion.
Two providers implement the same shape:

- `provider_id`: `"n8n-mcp"` (czlonkowski, existing) | `"official-n8n"` (new)
- `status() -> dict`
- `enable() -> dict`
- `disable() -> dict`
- optional `upgrade()` (czlonkowski only; official has no docs->full split)

Keep `managed` in the server record as the discriminator already used
(`managed: "n8n-mcp"`), adding `managed: "official-n8n"`. `get_registered()`
becomes provider-scoped. No change to the czlonkowski behaviour or its container
lifecycle — it is lifted as-is into `providers/n8n_mcp.py` (or left in place and
the official provider added alongside as `official_n8n_mcp.py`; either is fine,
the container logic must not be disturbed).

### 4.2 Official provider (`official_n8n_mcp.py`)

- `enable(mcp_token: str)` — takes the user-pasted **MCP Access Token** (spike
  confirmed the API key does not work; there is nothing to silently reuse):
  1. Resolve the active instance (`get_active_instance`). If none, return
     `{ok:false, error:"Connect an n8n instance first."}` — the official server
     has nothing to serve without one.
  2. Build the MCP URL from the instance URL: `<instance_url>/mcp-server/http`,
     running it through the same `dockerize_url()` localhost->host-gateway
     rewrite the n8n proxy uses, so a containerized dashboard can reach a
     host-published n8n. Path overridable via `AGD_OFFICIAL_N8N_MCP_PATH`.
  3. Encrypt the pasted token (`encrypt_value`) and register it as the server's
     `token`; the shared client sends it as `Authorization: Bearer` unchanged.
  4. Probe with `mcp_client.test_server`; only register on
     `connected && tools_count > 0`. Remediation copy keyed off the probe result:
     - `404` -> "This n8n version does not expose the official MCP server
       (needs a newer n8n)."
     - `401`/`403` -> "n8n rejected the token. In n8n, open Settings ->
       Instance-level MCP, enable MCP access, and copy the Access Token (this is
       NOT your n8n API key)."
- `disable()`: unregister only. There is no container to remove.
- `status()`: `{registered, url, instance_id, reachable, tools_count}` plus an
  `available` flag = "an instance is connected" (we cannot pre-probe without a
  token, so `available` gates only on instance presence; reachability is proven
  at enable time).
- No auto-install and no one-click on boot. The czlonkowski path auto-installs
  (`ensure_n8n_mcp`) because it owns the token it mints; the official path is
  paste-token-only because the token lives in n8n and the feature is admin-gated.

### 4.3 API surface

Generalize the existing subroutes under `/api/mcp/` from `n8n-mcp/*` to a
provider-parameterized form, keeping the old paths as aliases so nothing breaks:

- `GET  /api/mcp/providers` -> `[{id, name, status()...}]` for both providers
  (drives the card). New.
- `POST /api/mcp/providers/{provider_id}/enable` — body `{}` for czlonkowski,
  `{"mcp_token": "..."}` for official (the pasted MCP Access Token).
- `POST /api/mcp/providers/{provider_id}/disable`
- `POST /api/mcp/providers/n8n-mcp/upgrade` (czlonkowski only; 404 for official)
- Keep `/api/mcp/n8n-mcp/{status,enable,upgrade,disable}` as thin aliases to the
  czlonkowski provider (backward compat for the current Settings JS).
- `POST /api/mcp/compare` -> runs `discover_tools` against every registered
  managed provider and returns a normalized diff (see 4.5). New.

All stay `require_role("operator")` (unchanged; matches `mcp_router.py`).

### 4.4 Shared client: no change needed

(spike-confirmed) The official server authenticates with `Authorization: Bearer`,
which `mcp_client._headers()` already sends. The `auth_header` field proposed in
the draft is **dropped** — it was only there to cover a possible `X-N8N-API-KEY`
scheme that the spike ruled out. If a future third-party MCP server needs a
non-bearer header, add it then; it is not on this critical path.

### 4.5 Compare view

A new Settings panel section, "Compare n8n MCP providers", visible when at least
one managed provider is registered. It calls `POST /api/mcp/compare` and renders:

- Tool count per provider (live `tools/list`).
- Tool-name set diff: tools only-in-czlonkowski, only-in-official, in-both.
- A short static capability matrix (§3) so users see the qualitative axes the raw
  tool list does not show (validation strictness, template access, credential
  CRUD, pin-data).
- Optional "run a probe edit" is **out of scope for v1** (it would mutate a
  workflow); the compare is read-only tool introspection plus the static matrix.

### 4.6 UI: one card, two providers

Replace the single "Enable n8n intelligence" card in `settings.js` with a card
that lists both providers, each with its own status pill and
Enable/Disable/(Upgrade) buttons, plus a "Compare" button when >=1 is on. The
official row is disabled with an inline reason when `available` is false
("Connect an n8n instance"). When available, Enable opens a small token prompt
with a one-line how-to ("In n8n: Settings -> Instance-level MCP -> enable, then
paste the Access Token") and a link that opens the instance's settings in a new
tab.

## 5. Config / env

- `AGD_OFFICIAL_N8N_MCP_PATH` (default `/mcp-server/http`): override the endpoint
  path in case n8n changes it.
- No `AGD_OFFICIAL_N8N_MCP` auto-enable flag in v1: the feature is admin-gated in
  n8n and the token cannot be discovered, so there is nothing to auto-enable.
- Existing `AGD_N8N_MCP_*` (image, port, auto, url) unchanged.
- Secret handling: the pasted MCP Access Token is Fernet-encrypted at rest via
  `encrypt_value` (same as every other server token). It is a distinct secret
  from the instance API key, not a reuse of it.

## 6. Security

- Same operator-role floor as all `/api/mcp` routes.
- The official provider holds a **new** secret (the MCP Access Token), stored
  encrypted. It is scoped to n8n's MCP surface, separate from the REST API key.
- SSRF: the official URL is derived from an already-registered instance URL, so
  it passes through the same `assert_safe_probe_url` guard on probe/discover.
- The compare endpoint only lists tools; it never invokes them.

## 7. Open questions

1. ~~Auth shape.~~ **Resolved (spike):** `Authorization: Bearer` with a separate
   per-user **MCP Access Token** (not the REST API key, not `X-N8N-API-KEY`).
   No client change; paste-token enable flow.
2. **Minimum n8n version** that exposes `/mcp-server/http`. Confirmed present on
   2.28.5; lower bound TBD. The feature is admin-toggled (Settings ->
   Instance-level MCP), so "on by default" is No regardless. The `404`
   remediation copy covers the too-old case.
3. **n8n Cloud**: does Instance-level MCP / `/mcp-server/http` exist on n8n.cloud
   workspaces, or self-hosted only? Affects `available` for cloud-path users.
4. **Token lifecycle**: the MCP Access Token is per-user in n8n and can be
   rotated/revoked there. CE should surface a clear "n8n rejected the token,
   re-enable" state on a later 401 rather than silently dropping tools. Decide
   whether to actively re-probe on a schedule or only lazily on assistant use.
5. Whether to eventually let the assistant/agent-fleet pick a provider per run
   (out of scope here; both just register their tools into the shared pool today
   via `get_all_mcp_tools`, so having both on means both tool sets are offered —
   confirm that is desirable or add per-provider enable-for-assistant toggles).

## 8. Phasing

- **Phase 0 (spike): DONE (2026-07-02).** Confirmed `/mcp-server/http` on n8n
  2.28.5, `Authorization: Bearer`, separate MCP Access Token (admin-toggled). No
  client change required. Open Question 1 resolved; the "silent API-key reuse"
  and "auto-enable" ideas are killed.
- **Phase 1:** `official_n8n_mcp.py` provider (paste-token `enable(mcp_token)`,
  disable, status) + `/api/mcp/providers*` routes with aliases. No shared-client
  change. Tests mirroring `tests/test_n8n_mcp_provision.py` (mock the probe).
- **Phase 2:** Settings card refactor to two-provider layout + `available`
  gating and remediation copy.
- **Phase 3:** `/api/mcp/compare` + the compare panel (tool diff + static
  matrix).
- **Phase 4 (optional):** per-provider assistant toggles if Open Question 5
  lands that way, and a token-rejected re-enable prompt (Open Question 4).
  (No auto-enable-on-boot: ruled out by the admin-gated, undiscoverable token.)

## 9. Docs to update on ship

- `docs/guide/ai-assistant.md` and `docs/guide/code-lab.md` (both reference the
  MCP/n8n-mcp integration).
- `CHANGELOG.md` `[Unreleased] Added`.
- `CLAUDE.md` assistant-module line (note both providers).
