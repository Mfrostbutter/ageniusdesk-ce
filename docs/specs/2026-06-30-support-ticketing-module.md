# Spec: Support / Ticketing module (agency support inbox)

Status: SPEC / proposed. Not committed to a release.

Date: 2026-06-30

Related: `ROADMAP.md` ("Community Modules & Homelab Pack" → "modules that add
their own surface"), `2026-06-28-community-module-candidates.md` (buildability
quadrant + the credential-under-isolation gap), `2026-06-28-http-request-bridge.md`
(host-mediated outbound HTTP), `2026-06-30-consented-secret-tier.md` (the held-credential tier the IMAP
fast-follow rides), `2026-06-28-agent-fleet-langgraph-spec.md`
(consented-secret tier origin, "The credential fork"), `backend/modules/messages/`
(the inbound-webhook → persist → broadcast reference this extends).

## 1. What this is

A community module with **its own surface** — a dedicated ticketing inbox where
inbound client support email lands. Agencies running AgeniusDesk field support
over a shared address (`support@…`); this module turns that mailbox into a
first-class queue: each thread becomes a ticket with a status lifecycle, AI triage
and draft replies through the assistant, and optional cross-links to the workflow
or execution a request concerns.

It is **deliberately separate from Errors**. Errors are machine-generated workflow
failures; tickets are human requests. A ticket may *reference* an error, but it
does not live in the Errors feed. Two different feeds, two different audiences.

Distribution: **community module** (opt-in, installed via the GitHub inspect /
scan / consent pipeline), its own folder + manifest alongside `youtube-research`.
It pairs with the future multi-tenancy foundation for per-client routing and feeds
the client-reporting loop.

## 2. The architectural decision: intake mode decides isolation posture

This module's defining constraint is that it can hold a **mailbox credential**.
Per the candidates doc, a credential-holding module's isolation tier depends
entirely on *how* it gets its data. There are two intake modes and they sit on
opposite sides of the buildability line:

| Intake mode | Protocol | Credential? | Isolation today |
|---|---|---|---|
| **Forward-to-webhook** (recommended default) | HTTP POST into the module | **None** (token-gated webhook) | **Clean under all tiers** — mirrors `messages` exactly |
| **IMAP poll** | IMAP over TLS (stdlib `imaplib`/`email`) | Mailbox password | **`in_process`-only** until the consented-secret tier lands |

**Forward-to-webhook is the clean path and the v1 default.** The operator sets a
forwarding rule (or an n8n "watch mailbox" workflow) that POSTs each inbound
message to `/api/tickets/inbound`, token-gated by the install-time module token
exactly like the `messages` webhook. No credential ever enters the module, so it
runs sandboxed (subprocess/container) on day one with zero new host work.

**IMAP poll is the convenience path but hits the same wall as Redis/Postgres** —
not the *driver* wall (`imaplib` + `email` are stdlib, so it builds with no
dependency), but the **credential-under-isolation** wall. Under subprocess/
container the worker env is scrubbed, so a module that must `imaplib.login(user,
password)` cannot get the password unless the host injects it. That is exactly the
**consented-secret tier** now specced at `2026-06-30-consented-secret-tier.md`
(`capabilities.worker_secrets`, AST-scanner HIGH, separate consent gate, both env
builders inject only the declared+consented secrets, container-first). The
`http.request` bridge does **not** help here — IMAP is not HTTP.

**Decision:** ship **webhook intake in v1** (sandboxed, no host investment). Add
**IMAP poll as a fast-follow** that rides the shared consented-secret tier once it
exists — the same host capability Proxmox and any keyed module want, so the
ticketing module is a second customer for it, not the reason to build it.

### 2.1 Inbound webhook payload (v1 contract)

`intake.py` normalizes a **pre-parsed JSON object**, not raw RFC-5322. v1 targets
JSON because the realistic producers — an n8n "watch mailbox" / "email trigger"
workflow, or a provider's inbound-parse webhook (Mailgun/Postmark/SendGrid) — all
emit parsed fields already; making the module a full MIME parser is scope the
fast-follow's IMAP path will own (`email` stdlib) but v1 should not. The IMAP
fast-follow produces the same normalized object internally, so `intake.py` has one
input shape regardless of source.

`POST /api/tickets/inbound` (token-gated by the install-time module token, exactly
like `messages`):

| Field | Required | Notes |
|---|---|---|
| `from` | yes | requester address; `from_name` optional |
| `subject` | yes | may be empty; threading falls back to it |
| `body_text` | yes* | plaintext body. `body_html` accepted and stripped to text if `body_text` absent |
| `message_id` | yes | RFC-5322 `Message-ID` of this message; the dedup key |
| `in_reply_to` | no | `In-Reply-To` header |
| `references` | no | `References` header (space-separated chain) |
| `received_at` | no | ISO-8601; defaults to host receive time |

Missing `message_id` (some forwarders strip it) → the module synthesizes a stable
one from `hash(from + subject + received_at)` so dedup and threading still function.

### 2.2 Threading + dedup rule (v1)

- **`thread_key`** = the **root Message-ID** of the conversation: if `in_reply_to`/
  `references` are present, take the first (root) id of the `references` chain (or
  `in_reply_to` when `references` is absent); otherwise this message starts a thread
  and `thread_key` = its own `message_id`. Subject is the **last-resort** fallback
  only when no id headers exist at all: normalize (strip `re:`/`fwd:`, collapse
  whitespace, lowercase) and key on `requester_email + normalized_subject`.
- **Create-or-append:** look up an open/pending ticket by `thread_key`; append the
  message if found, else create a new ticket. A `resolved`/`closed` ticket whose
  thread gets a new inbound message **reopens** (status → `open`) rather than
  spawning a duplicate.
- **Dedup:** before insert, reject a `message_id` already present in
  `ticket_messages` (a forward rule + an n8n watcher both delivering the same mail
  is a real double-delivery path). `message_id` is unique-indexed on intake.

## 3. Outbound (replies): three options, prefer the credential-free one

Sending a reply is the symmetric problem — it needs a way out to the mail
provider. Ranked by how cleanly they fit isolation:

1. **Reply via n8n (recommended default).** The module POSTs the drafted reply to
   an operator-provided n8n webhook (a "send support email" workflow). Mail-sending
   credentials stay in n8n, where the operator already keeps them; the module holds
   **nothing**. Most on-brand for this product (n8n is already the fleet's spine)
   and isolation-clean. The reply workflow URL is a normal module setting.
2. **Reply via REST mail API** (Postmark / SendGrid / Resend / Mailgun) through the
   **`http.request` bridge**. The send credential stays host-side; the module calls
   `host.http.request({endpoint: "mail", method: "POST", …})`. Isolation-clean once
   the bridge ships. A declared, operator-consented mutating endpoint (POST → the
   stronger consent text from the bridge spec §6).
3. **Reply via SMTP** (stdlib `smtplib`). Native-wire, holds an SMTP credential →
   `in_process`-only, same posture as IMAP poll. Offered only as a fallback for
   operators who want self-contained mail without n8n or a mail API.

v1 ships option 1 (n8n reply workflow) and option 2 (REST mail via bridge, if the
bridge has landed). SMTP is the `in_process` fallback.

**Reply identity (v1 contract).** The module **holds no sending identity.** It POSTs
`{to, subject, body, ticket_id, thread_key, in_reply_to}` to the operator-configured
reply target (the n8n webhook, or the `mail` REST endpoint); the **n8n workflow / mail
endpoint owns the From address, signature, and per-client branding.** This keeps the
module credential-free and identity-free in v1. Per-client From/signature handled
*inside* the module is deferred to multi-tenancy (the module would then pass
`client_tag` and let the workflow map it). `thread_key`/`in_reply_to` are passed
through so the operator's workflow can set the outbound `In-Reply-To`/`References`
headers and keep the mail thread intact.

## 4. AI triage + draft replies

Both run through the **existing `assistant.complete` bridge** — no new host
surface. `assistant.complete` is one-shot and tool-free, which is exactly right
here (triage is a single classification/summarization call; a draft reply is a
single generation). The LLM key never enters the worker.

- **Triage on intake**: classify priority (low/normal/high/urgent), suggest a
  category/tag, summarize the request to one line, and optionally guess the
  client/tenant. Stored on the ticket; never auto-sent.
- **Draft reply**: operator clicks "Draft reply"; the module composes a suggested
  response from the thread + any linked workflow/error context. **Always
  human-reviewed before send** — the module drafts, the operator sends. No
  auto-reply in v1 (an autoresponder is an explicit, separately-consented setting
  if added later).

**Triage runs as an async backfill, not on the intake path.** Intake creates the
ticket immediately from the parsed payload, then triage (`priority`/`ai_summary`/
`category`/`client_tag`) is filled in by a follow-up task. So a slow or failing LLM
call never delays or drops an inbound message — the ticket exists with defaults
(`priority=normal`, empty summary) and upgrades when triage returns.

**Client-tag privacy note.** The v1 client guess sends the **sender domain** into the
triage prompt. That is acceptable for the single-inbox v1, but the right long-term
shape is a **host-side `domain → client_tag` lookup**, not an LLM guess — the
domain→client map is operator-managed in settings, deterministic, and keeps tenant
identity out of the model. When multi-tenancy lands, replace the LLM guess with that
lookup; do not bake the LLM path in as the permanent mechanism.

## 5. Cross-links to the fleet

A ticket can optionally reference what it is *about*:

- `linked_workflow_id` + `linked_instance_id` — the workflow a complaint concerns.
- `linked_execution_id` — a specific run.
- `linked_error_id` — a row in the Errors feed (the one direction the separation
  allows: a ticket points *at* an error; the error does not absorb the ticket).

These are soft references (IDs + a label snapshot), resolved for display by calling
the relevant module's read API. They never create a hard FK across module DBs.

## 6. Data model (module-owned DB)

Following the module-owned-storage precedent (own your storage rather than touching
`dashboard.db`), tickets live in a **module-private `tickets.db`** in the worker's
own data dir — `AGD_MODULE_DATA_DIR/tickets.db` (`data/modules/support-tickets/
_data/` under isolation), the one path a sandboxed worker may write outside the
bridge (isolation spec §5.6). In the `in_process` dual-mode path the facade resolves
the same dir. (Agent Fleet sits at `data/agentfleet.db` because it went *core*/
in-host; a community worker uses its private data dir.) Two tables:

**`tickets`**
| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `subject` | TEXT | |
| `status` | TEXT | `new` → `open` → `pending` → `resolved` → `closed` (+ `snoozed`) |
| `priority` | TEXT | `low`/`normal`/`high`/`urgent` (AI-suggested, operator-editable) |
| `requester_email` | TEXT | |
| `requester_name` | TEXT | |
| `client_tag` | TEXT | per-client routing (nullable until multi-tenancy) |
| `assignee` | TEXT | local user id, nullable |
| `category` | TEXT | AI-suggested tag |
| `ai_summary` | TEXT | one-line triage summary |
| `linked_instance_id` | TEXT | nullable cross-link |
| `linked_workflow_id` | TEXT | nullable |
| `linked_execution_id` | TEXT | nullable |
| `linked_error_id` | INTEGER | nullable; the local `errors.id` (dashboard.db, `INTEGER PRIMARY KEY`). Soft ref resolved at display via the errors read API — no cross-DB FK; a stale id just renders unlinked |
| `thread_key` | TEXT | root Message-ID of the conversation (see §2.2); the threading key |
| `created_at` / `updated_at` | TEXT | UTC `%Y-%m-%d %H:%M:%S`, matching house convention |

`message_id` is stored on `ticket_messages` (below) with a UNIQUE index for dedup
(§2.2). The Errors PK is confirmed a stable integer (`backend/database.py` `errors`
table), so `linked_error_id INTEGER` is correctly typed; it is still a *soft* ref,
not an enforced FK.

**Status transitions (v1).** Operator-driven, with two automatic edges:
`new → open` on first operator view (or immediately, operator preference);
`open ⇄ pending` (waiting on requester) and `→ resolved → closed` are manual;
`snoozed` is **non-terminal** — it auto-returns to `open` at a set time or on a new
inbound message. A new inbound message on a `resolved`/`closed` thread **reopens**
to `open` (§2.2). Any state may be set to `closed` by an operator (e.g. spam:
`new → closed`). The frontend offers only the forward edges plus close; the service
accepts operator-driven transitions and stamps `updated_at`.

**`ticket_messages`** (the thread)
| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `ticket_id` | INTEGER FK | |
| `message_id` | TEXT | RFC-5322 id (or synthesized, §2.1); **UNIQUE** for dedup |
| `direction` | TEXT | `inbound` / `outbound` / `note` (internal) |
| `author` | TEXT | requester email or local user |
| `body` | TEXT | plaintext (HTML stripped on intake), capped at 256 KB on intake |
| `is_draft` | INTEGER | 1 = AI draft awaiting send |
| `created_at` | TEXT | UTC |

**Notification on intake/status change.** The module emits a host toast on a new
ticket / status change by reusing the `messages` webhook path (`POST /api/messages/
webhook` through its egress), so the event reaches any **core** surface listening on
`/ws`. The module's own sandboxed iframe does **not** get a push — it reflects the
change on its next poll (a few seconds; §8). So: live toast for the core shell,
poll-latency refresh for the ticket inbox itself. (A community module's worker cannot
call `manager.broadcast` directly; that is a host object, and the `broadcast` bridge
namespace is unbuilt — §5.5c of the isolation spec.)

**Retention (v1).** No auto-purge. Tickets persist until an operator deletes them;
thread bodies are capped at 256 KB on intake (above). A configurable
archive/retention policy is a fast-follow, not v1. Stated so an unbounded shared
inbox is a known, chosen v1 behavior, not a surprise.

## 7. Module layout (`modules/support-tickets/`)

```
manifest.json    id support-tickets; routes_prefix /api/tickets; capabilities:
                 network egress (reply-via-n8n / mail API), host.assistant
                 (triage+draft), host.http endpoint "mail" (optional, reply via
                 REST). NO filesystem write_paths needed — write_paths are
                 VAULT-relative; tickets.db lives in the module-private data dir
                 (AGD_MODULE_DATA_DIR), not the vault. NO worker_secrets in v1
                 (webhook intake). IMAP fast-follow adds worker_secrets.
__init__.py      re-exports router
router.py        APIRouter(prefix="/api/tickets"); inbound webhook (token-gated);
                 list/get/update tickets; post reply/draft; status transitions
service.py       ticket + thread persistence (tickets.db); threading by thread_key
intake.py        normalize inbound payload → ticket/message; HTML→text; triage call
reply.py         draft (assistant.complete) + send (n8n webhook / http.request / SMTP)
_host.py         dual-mode facade (assistant_complete + http_request via bridge or direct)
static/
  tickets.html   inbox list + ticket detail (thread, status controls, draft/send)
  tickets.js     polls AgeniusDesk.fetch; renders queue + thread
README.md  tests/
```

## 8. Frontend

A community module's view runs in the **sandboxed opaque-origin iframe** whose
only host channel is `AgeniusDesk.fetch` (buffered, no streaming). So the frontend
is **polling**, the established CE community pattern (`youtube-research`,
agent-fleet-as-community before it went core): poll the ticket list every few
seconds while the inbox is open; poll an open ticket's thread on the same cadence.
No SSE, no direct `/ws` subscription from the iframe. The host toast (§6) is for the
core shell; the ticket inbox itself updates at poll latency, not via push — don't
read "live toast" as the iframe getting a real-time feed.

Surface: a two-pane inbox (queue on the left filtered by status/priority/client,
thread on the right) with status controls, a "Draft reply" button, an editable
draft box, and a "Send" action. `frontend.nav` adds a **Support** / **Tickets**
entry with an open-count badge.

## 9. Consent + scanner posture

- **Webhook intake (v1)**: declares `host.assistant` (triage/draft) and network
  egress (reply path). Holds no secret → no consented-secret finding. The inbound
  webhook is token-gated by the per-install module token, same as `messages`.
- **Reply via `http.request`**: the `mail` endpoint is a **mutating (POST)
  endpoint** → the stronger per-endpoint consent text from the bridge spec §6.
- **IMAP poll (fast-follow)**: declares `worker_secrets` (the mailbox password) →
  AST-scanner **HIGH** ("this module receives your mailbox credential in its
  sandbox"), explicit separate consent at install. SMTP reply has the same posture.
- Reading/parsing email is untrusted input: HTML is stripped to text on intake, no
  remote content is fetched, **attachments are dropped in v1** (storing them as
  opaque blobs is a fast-follow alongside IMAP), and the triage prompt treats the
  body as data, not instructions.

## 10. Multi-tenancy / per-client routing

v1 is a **single shared inbox** with an optional `client_tag` (AI-guessed from
sender domain, operator-editable) and queue filtering by tag. Full per-client RBAC
(an agency user sees only their client's tickets, scoped reporting) **defers to the
multi-tenancy foundation** on the medium-term roadmap. Routing rules (map an
inbound address / domain / subject pattern → `client_tag` + assignee) are a thin
settings table that lands with multi-tenancy, not before.

## 11. Relationship to Fleet Health

This module **adds its own surface**; it does not fold into Fleet Health. Once the
contribution API (`2026-07-01-fleet-health-contribution-api.md`) ships, the module
declares `"fleet_health": {route: "fleet-health"}` and serves a `{rows:[{label:
"Support", status, metrics:[{label:"open",…},{label:"urgent",…}]}]}` summary the host
pulls. Until then the open-ticket count is surfaced only on the module's own nav
badge and optionally an Overview stat card.

## 12. Build order

1. Scaffold `modules/support-tickets/` (manifest, `__init__`, `_host` from
   `youtube-research`).
2. `tickets.db` schema + `service.py` (CRUD, threading by `thread_key`).
3. Inbound webhook (`/api/tickets/inbound`) + `intake.py`: normalize the §2.1 JSON
   payload, HTML→text, dedup on `message_id`, thread + create-or-append per §2.2;
   emit the host toast via the `messages` webhook. (Mirror `messages` collector.)
4. Triage as an **async backfill** (not on the intake path) via `assistant.complete`
   (priority/summary/category; client guess from sender domain — see §4 privacy
   note). A triage failure leaves the ticket at defaults; it never drops the message.
5. Reply: draft via `assistant.complete`; send via n8n reply-workflow webhook
   (option 1). Add the `http.request` mail endpoint (option 2) if the bridge has
   landed.
6. Frontend: polling inbox (list + thread + status controls + draft/send), nav
   entry with open-count badge.
7. Tests: inbound normalization + threading; status transitions; triage call
   shape (stub assistant); reply send (stub n8n/mail); HTML-strip + untrusted-input
   handling.
8. Dogfood: forward a real `support@` address at the webhook, watch a thread become
   a ticket, triage, draft a reply, send via an n8n workflow, resolve.
9. **Fast-follow (separate slice, shared host work):** IMAP poll intake +
   `worker_secrets` consented-secret tier; SMTP reply fallback.

## 13. Resolved for v1 + remaining open questions

**Resolved (locked for build):**

- **Inbound payload** — pre-parsed JSON, not raw MIME (§2.1).
- **Threading + dedup** — root-Message-ID `thread_key`, subject last-resort, dedup
  on `message_id` UNIQUE, reopen-on-reply (§2.2).
- **Reply identity** — module holds none; the n8n workflow / mail endpoint owns
  From/signature; `thread_key`/`in_reply_to` passed through (§3).
- **Triage timing** — async backfill, never blocks intake (§4).
- **Attachments** — dropped in v1; blobs are a fast-follow (§9).
- **Status transitions** — operator-driven with the auto edges in §6.
- **Retention** — no auto-purge; 256 KB body cap on intake (§6).

**Still open:**

- **Threading edge case**: a forwarder that rewrites BOTH `Message-ID` and the
  references chain defeats id-based threading and falls to the subject heuristic;
  how aggressively to merge near-duplicate subjects without cross-joining unrelated
  threads. v1 ships the conservative rule (exact normalized subject + same
  requester); tune from real traffic.
- **Autoresponder / SLA timers**: out of v1; if added, the autoresponder is a
  separately-consented setting and SLA timers pair with multi-tenancy reporting.
- **Per-client From/signature inside the module** (vs the workflow owning it) —
  revisit with multi-tenancy.

## 14. Out of scope for v1

- IMAP poll intake and SMTP reply (fast-follow, needs the consented-secret tier).
- Per-client RBAC and scoped reporting (defers to multi-tenancy foundation).
- Auto-reply / autoresponder and SLA timers.
- Fleet Health row (needs the contribution API — now specced at
  `2026-07-01-fleet-health-contribution-api.md`, but a separate build).
- Live (non-polled) inbox — the sandboxed iframe is buffered; polling is the
  contract unless/until this goes core.
