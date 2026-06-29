# Community Module Candidates — landscape, buildability, and host gaps

Status: FINDINGS / scoping. Captures the 2026-06-28 brainstorm + architecture
grounding so the build is fast when we pick modules. Nothing here is committed to a
release.

Date: 2026-06-28

## Frame

AgeniusDesk already has the chrome every operator dashboard wants and few have
together: the **Fleet Health** rollup, the **Errors** feed, **Ask AI**, **Observe**
(OTel), and the **Notes/Knowledge** vault. The highest-value community modules are
not standalone apps; they are integrations that **fold into those surfaces**. That
reframes AgeniusDesk from "an n8n control plane" toward "the homelab / automation
control plane."

Distribution vehicle for a curated set: the existing **bundle** concept
(`docs/community-templates/bundle-schema.md`) — one install, many modules.

## Two architectural facts that decide buildability

**Fact 1 — protocol determines cleanliness.** The installer mounts module *code*;
it does **not** `pip install` anything (`backend/modules/modules/installer.py`). A
module gets stdlib plus what the host already ships (httpx, aiosqlite, pydantic,
fastapi). So:

- **REST/HTTP** integrations → bundled **httpx** → clean.
- **Native-wire** integrations (Redis RESP, Postgres/MySQL wire) → need a driver
  that isn't present → must vendor it, speak the protocol over a **raw socket**
  (which the AST scanner flags HIGH), or require the operator to install it.

**Fact 2 — three host-side gaps cap the experience.** None live in the module;
only the host can close them:

1. **No credential path under isolation.** The capability bridge
   (`backend/modules/_runtime/bridge.py`) exposes exactly two namespaces:
   `notes.*` and `assistant.complete`. There is no secret-fetch and no
   authenticated-HTTP capability. Under `subprocess`/`container` the worker env is
   scrubbed. So **any module holding an external credential runs `in_process` only
   today** — full host access, no containment. *This is the dominant blocker.*
   Closed by the **`http.request` bridge** (see
   `2026-06-28-http-request-bridge.md`).
2. **Fleet Health is n8n-only.** `fleet_health()` lives in `n8n_proxy` and
   aggregates n8n instances. There is **no contribution API** for a module to
   publish a health row, so every "folds into Fleet Health" claim needs a new host
   extension point first. (Future investment — not yet specced.)
3. **No dependency install.** Restatement of Fact 1 as a host gap: there is no
   requirements step in the installer.

## Buildability verdict — Tier 1 / Tier 2

| Module | Verdict | Why |
|---|---|---|
| **Cloudflare** | Compatible | REST + Bearer over httpx. Cleanest. Only the credential-under-isolation gap. |
| **NocoDB / Baserow / Airtable** | Compatible | REST + token, httpx. |
| **Qdrant vector ops** | Compatible | REST on :6333, httpx. Companion to Knowledge. |
| **Remote Docker / Portainer** | Compatible | Docker Engine API + Portainer are HTTP. Use Portainer REST or the daemon TLS HTTP API; do NOT reuse the local-socket SDK path. |
| **Object storage (S3 / MinIO / B2)** | Questionable | S3 is HTTP but SigV4 without boto3 is fiddly (doable via stdlib hmac/hashlib). MinIO/B2 native APIs simpler. Compatible-with-effort. |
| **Uptime Kuma** | Questionable | Live API is socket.io, not clean REST. Read-only connector works via Prometheus `/metrics` + status-page JSON; awkward shape. |
| **Redis / queue monitor** | Questionable | Needs a Redis driver (not bundled) or raw-socket RESP (HIGH finding). Credential-holder → in_process-only. Buildable, not clean. |
| **Database viewer (Postgres/MySQL)** | No-good as community (today) | Triple-blocked: wire driver not delivered; holds DB superuser creds → in_process-only; raw DB access with full host reach is the exact trust profile isolation exists to prevent. Better as a **built-in**, or defer. |

Net: the **HTTP/REST quadrant is shippable now** (Cloudflare, NocoDB-family,
Qdrant, Remote-Docker-via-HTTP). The **native-protocol quadrant** (Redis, Postgres)
is where the architecture pushes back.

## Homelab Pack

Homelab control planes are **HTTP almost everywhere**, so the pack sits almost
entirely in the buildable quadrant — gated by the host investments, not by protocol.

| Module | Verdict | Note |
|---|---|---|
| **Proxmox** | Compatible | REST/token on :8006; self-signed TLS (needs per-endpoint verify=false); self-protection (never stop/reboot the node or the VM/LXC the dashboard runs on). |
| **Home Assistant** | Compatible | REST/token on :8123. Live state is a WebSocket: the module backend holds the WS (or polls) and re-streams to its iframe via SSE on `/api/{id}/...` — never a direct browser→HA socket. |
| **Cloudflare** | Compatible | REST/token. |
| **Reverse proxy (NPM / Traefik / Caddy)** | Compatible | all expose REST/admin APIs. |
| **Pi-hole v6 / AdGuard Home** | Compatible | REST APIs (Pi-hole pre-v6 is weaker). |
| **Tailscale / NetBird** | Compatible | REST + token. |
| **TrueNAS** | Compatible | REST v2. |
| **Unraid** | Questionable | only the newer Connect GraphQL API; maturity caveat. |
| **Authentik / Authelia** | Compatible / Questionable | Authentik full REST; Authelia thin API. |

**Pack v1 core** (high demand + clean Fleet-Health fold): Proxmox, Remote
Docker/Portainer, NAS health (TrueNAS), Uptime Kuma, Cloudflare, Home Assistant.
**Extended:** reverse proxy, Pi-hole/AdGuard, Tailscale/NetBird, Authentik.

## Confirmed direction (2026-06-28)

- **Agent Fleet** — shipped as a core built-in (`backend/modules/agent_fleet/`).
  No longer "scoping."
- **Proxmox** — committed community module.
- **Secret backends (Infisical / agent-vault)** — committed; the inverted-bridge
  design question still stands (see `2026-06-28-integration-modules-roadmap.md`).
- **Scheduled backups** — committed (low-hanging; near-term).
- **Homelab Pack** — adopted as a direction; bundles the modules above.

## The two host investments (priority order)

1. **`http.request` bridge** — host-mediated outbound HTTP scoped to the module's
   declared endpoints, host injecting the consented credential. Converts the entire
   credential-holding REST quadrant (Cloudflare, NocoDB, Qdrant, object storage,
   Uptime Kuma, **Proxmox, Home Assistant**, and the rest of the homelab pack) from
   `in_process`-only into safe-under-isolation. **Highest leverage; spec at
   `2026-06-28-http-request-bridge.md`.**
2. **Fleet Health contribution API** — a registry where a loaded module publishes
   `{label, status, metrics}` rows that `fleet_health()` merges, so module health
   actually renders in the pane the pack is pitched around. (Not yet specced.)

Redis/Postgres additionally need a **dependency policy** (vendor / optional extra
like the `langgraph` extra / operator-installs) before they are clean — part of why
a DB viewer is better as a built-in.

## Sequencing argument

Build the `http.request` bridge **before** the modules. It is one host change that
unlocks the whole HTTP/REST + homelab quadrant at once, on the same dual-mode
pattern as `assistant.complete`. Proxmox and Home Assistant are then not special
cases — they are two more REST modules.
