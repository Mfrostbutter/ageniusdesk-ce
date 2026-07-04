# Spec: Support Ticket Module (agency support desk)

Status: DRAFT / build spec
Date: 2026-07-03
Owner: Michael Frostbutter

Community module for AgeniusDesk. Turns client support email into tickets that are
linked to the operational objects they concern (client, instance, workflow,
execution), escalates real failures into tracked issues, and feeds resolutions back
into client reporting.

## 1. Why, and the principle that drives the design

An agency running a fleet of n8n instances gets support requests by email. Dropping
those into "yet another inbox" loses the thing that matters: the request is about a
specific client's specific workflow that failed a specific way. The value is not
co-locating email in the dashboard, it is **linking each request to the operational
object it belongs to** and keeping a durable trail.

Principle (locked): a support item is only useful when it carries its context.
Every ticket answers, by the time it closes: what the client reported, which
client/instance/workflow/execution it touched, what actually changed, and whether it
should appear in that client's reporting later. The trail is a first-class entity,
not an afterthought. This spec makes the linkage and the trail the backbone, and
treats the inbox as the least interesting part.

## 2. The lifecycle (maps directly to the requested flow)

1. **Client email becomes a ticket.** Inbound email lands via webhook and creates a
   `ticket` (open, unlinked).
2. **Ticket links to client, instance, workflow, execution if known.** A linking
   pass auto-resolves what it can (sender domain to client, client to instance,
   references in the body to a workflow/execution/error) and proposes the rest for
   one-click confirm.
3. **If it is a real failure, create or link to an issue.** From the ticket, an
   operator promotes to a new `issue` or attaches to an existing open one. Dedupe is
   keyed on the same grouping the Errors feed already uses
   (instance + workflow + node + error_type), so N clients reporting one outage
   converge on one issue.
4. **The issue gets owner, severity, root cause, resolution.** Issue is the
   operational record. It carries those fields and its own status.
5. **Resolution summarizes back to the client and into the next maintenance
   report.** On resolve, a client-safe summary propagates to every linked ticket, an
   operator sends it back to the client (LLM-drafted, human-approved), and the issue
   is flagged for that client's reporting window.

## 3. Relationship to what already exists

- **Errors module** (`backend/modules/errors/`): the raw failure feed and its
  grouped view are the substrate for issues. An issue references an error group key
  or a specific `errors.id` / `execution_id`. We do not duplicate error storage; we
  point at it.
- **Messages module**: the inbound-webhook-plus-WebSocket-broadcast pattern is the
  template for email intake and live ticket toasts.
- **n8n_proxy / instances**: `instance_id` is the join key throughout, same as
  errors. Workflow and execution references reuse n8n identifiers.
- **Insights**: the maintenance-report feed is a natural companion; insights already
  aggregates execution analytics per instance.
- **Agent Fleet**: optional AI triage dogfoods the existing LangGraph runner, same
  pattern the contribution-pipeline spec uses for `gh-issue-triage`.
- **NOT the Jira contribution pipeline.** `2026-06-28-contribution-pipeline-and-jira-sync.md`
  is about AgeniusDesk's own GitHub issues. A support "issue" here is a client
  operational failure, unrelated to GitHub. Keep the vocabulary distinct.

## 4. Core model

Four entities. Tickets and issues are many-to-one (many client tickets, one
operational issue). Clients own instances. The trail records everything.

```
client 1───* ticket *───1 issue *───1 (error group / execution)
   │             │
   *             *
instance     ticket_message (thread)      ticket_event / issue_event (trail)
```

### Ticket (client-facing request)
The unit of client communication. Cheap to create, links accrete over its life.

| Field | Notes |
|---|---|
| id | pk |
| client_id | nullable until linked |
| instance_id | nullable until linked |
| workflow_id / workflow_name | nullable |
| execution_id | nullable |
| error_id | nullable FK into `errors.id` |
| issue_id | nullable FK into `support_issues.id` |
| source | `email` / `manual` / `webhook` |
| from_email / from_name | sender |
| subject | |
| status | `new` / `open` / `pending` / `resolved` / `closed` |
| priority | `low` / `normal` / `high` / `urgent` |
| created_at / updated_at | |

### Ticket message (thread)
Inbound and outbound messages on a ticket, so the reply history is preserved.

| id | ticket_id | direction (`in`/`out`) | body | author | at | delivery (`draft`/`sent`/`failed`) |

### Issue (operational failure record)
The durable operational object. One issue, many tickets.

| Field | Notes |
|---|---|
| id | pk |
| title | |
| instance_id / workflow_id | |
| error_group_key | `instance|workflow|node|error_type` (matches Errors grouping) |
| owner | dashboard user |
| severity | `critical` / `high` / `medium` / `low` |
| status | `open` / `investigating` / `resolved` / `wontfix` |
| root_cause | operator text |
| resolution | operator text (internal) |
| client_summary | client-safe version of the resolution |
| include_in_report | bool, per issue |
| created_at / resolved_at | |

### Client
Lightweight. An agency groups instances under a client and matches inbound email by
domain.

| id | name | email_domains (json list) | instance_ids (json list) | reporting_enabled |

Client is optional at intake; a ticket can sit unassigned until triaged. A client
can be inferred from the sender domain when a match exists.

### Trail (the durable record)
`ticket_event` and `issue_event`: append-only audit rows.

| id | ticket_id/issue_id | actor | event_type | detail (json) | at |

Event types: `created`, `linked_instance`, `linked_workflow`, `linked_execution`,
`linked_error`, `issue_created`, `issue_linked`, `status_change`, `reply_drafted`,
`reply_sent`, `resolved`, `flagged_for_report`. This is what makes "what changed and
when" answerable months later.

## 5. Data model (module-owned schema)

The module owns `support_*` tables. It creates them idempotently on mount via the
shared `get_db()` connection with `CREATE TABLE IF NOT EXISTS`, the same shape as
the host's `database.py` migrations but namespaced to the module. Tables:
`support_clients`, `support_tickets`, `support_ticket_messages`, `support_issues`,
`support_ticket_events`, `support_issue_events`. Foreign, non-owned references
(`errors.id`, `execution_id`, `instance_id`) are stored as plain columns, not FKs,
because errors rows can be cleared independently; a dangling reference degrades to
"error record no longer retained," never a crash.

Host-schema note: owning tables in the host SQLite means the module needs direct
`get_db()` access, which is an `in_process` capability (see section 12). Flag for
the host: a formal module-migration hook would be cleaner than each module running
its own `CREATE TABLE` on mount. Not blocking; the pattern works today.

## 6. Email intake

**v1: inbound webhook (no outbound credentials, sandbox-clean intake).**
`POST /api/support/intake` accepts a parsed email:
```json
{ "from": "ops@client.com", "from_name": "Client Ops", "subject": "...",
  "body": "...", "message_id": "...", "in_reply_to": "...", "instance_hint": "..." }
```
Auth: the host's X-API-Key surface (`public_api`) or the webhook-token middleware,
same posture as the errors/messages webhooks. The agency points any email-to-webhook
source at it: an n8n workflow with an email trigger (the dogfood path, and what an
agency already runs), or a provider inbound-parse (Postmark, SendGrid, Mailgun).
Threading: `in_reply_to` / `message_id` append to an existing ticket instead of
opening a new one.

**Later: IMAP poll.** A built-in poller pulls a support mailbox directly. This holds
mailbox credentials, so it is an outbound-credential feature: `in_process` with
consented creds, or via the planned `http.request` bridge. Deferred out of v1 for
that reason.

## 7. Linking engine (the useful part)

On intake, run a best-effort linking pass and record confidence. Auto-link at high
confidence, propose at low, always leave a trail row.

- **Client:** match `from` domain against `support_clients.email_domains`. One match
  auto-links; multiple or none leaves it for triage.
- **Instance:** from the client's `instance_ids`; if exactly one, auto-link.
- **Workflow / execution:** scan subject and body for an execution id, a workflow
  name that matches a known workflow on the linked instance, or a URL that embeds
  either. n8n execution URLs carry the id; parse them.
- **Error / issue:** within a recent window on the linked instance, match the
  referenced workflow (and error text similarity) against the Errors feed and open
  issues. Propose the top candidate issue to attach to; do not auto-attach (that is
  an operator judgment).

Every link, auto or manual, writes a `ticket_event`. Nothing links silently.

## 8. Issue management

- **Create or link from a ticket.** "This is a real failure" opens a dialog:
  attach to an existing open issue (dedupe candidates shown, keyed on
  `error_group_key`) or create a new issue seeded from the ticket + linked error.
- **Dedupe reuses Errors grouping.** Same `instance|workflow|node|error_type` with
  an open issue already present means link, not duplicate. This is the same
  convergence the Errors grouped view already does, extended to client reports.
- **Fields.** Owner, severity, status, root_cause, resolution, client_summary,
  include_in_report. Editing any writes an `issue_event`.
- **Fan-out on resolve.** Resolving an issue sets `resolved_at`, propagates
  `client_summary` to every linked ticket as a proposed outbound reply (draft, not
  sent), and marks linked tickets `resolved` pending the client reply.

## 9. Client reply and outbound

Replies to clients are **human-gated**. The module drafts, a person approves and
sends.

- **Draft:** LLM via the `assistant.complete` bridge (key stays host-side) turns the
  issue `client_summary` plus ticket context into a client-appropriate reply. Marked
  as a draft `ticket_message` (`direction=out`, `delivery=draft`).
- **Send paths:**
  1. **n8n relay (default for agencies):** `POST` the approved reply to an operator-
     configured n8n webhook that sends from the agency's real support address. Keeps
     email infrastructure where the agency already runs it, and the control plane
     stays orchestration, not an SMTP server.
  2. **Host SMTP:** reuse the existing `agd_smtp_*` settings (already used for
     password reset) for a simple direct-send path.
- On send, the message flips to `sent` (or `failed`), and a `reply_sent` trail row
  is written. No client email is ever sent without an explicit operator action.

## 10. Maintenance report feed

The reporting linkage Michael called out. The module does not render the report; it
exposes the durable, client-safe feed a report generator (or an n8n workflow, or a
future maintenance-report module) consumes.

`GET /api/support/report-feed?client_id={id}&from={date}&to={date}` returns, for
issues resolved in the window with `include_in_report=true` on that client's
tickets:
```json
[ { "issue_id": 12, "title": "...", "workflow_name": "...", "severity": "high",
    "reported_at": "...", "resolved_at": "...", "client_summary": "...",
    "tickets": [ {"subject": "...", "from": "ops@client.com"} ] } ]
```
Shape answers the four report questions per item: what was reported, what workflow
it affected, what changed (client_summary), and that it is cleared. A downstream
report step formats this into the client's maintenance report.

## 11. AI assist (optional, dogfoods the Agent Fleet)

Advisory only, reversible, same guardrails as the contribution-pipeline triage spec
because inbound email is attacker-influenced text.

- **`support-triage` agent:** input is a new ticket (subject, body, sender, linked
  instance). Output is proposed priority, a proposed client/instance/workflow link
  set, an is-this-a-failure verdict with a candidate issue, and a drafted first
  reply. Posts suggestions to the ticket; never sends, never resolves.
- **Guardrails:** treat email content as data, never instructions. Fixed tool
  allowlist (propose-link, propose-label, draft-message). Output validation against
  known clients/instances/issues; unknown proposals dropped. No secrets to the
  model; LLM key host-side via the bridge. Every AI action is visibly labeled and
  operator-reversible.

## 12. Isolation and distribution posture

Honest fit, same reasoning used for the secret-backend module:

- The module reads host operational data (errors, instances, executions) and owns
  tables in the host SQLite. A sandboxed worker (subprocess/container) cannot reach
  `get_db()` or the errors feed. So it runs **`in_process`**.
- It ships as a **community module** installed through the inspect/scan/consent
  pipeline, but the consent screen must disclose: reads host DB and error data,
  creates `support_*` tables, sends outbound email on operator action. This is the
  same "consented in-process" tier the secret backends need.
- **Folds into existing surfaces** (the community-module thesis): issues cross-link
  the Errors feed; open-ticket and open-issue counts per client become Fleet Health
  rows once the Fleet Health contribution API exists (not yet built, tracked as a
  host gap); resolutions can reference Notes runbooks.

## 13. Security

- **Inbound email is untrusted.** Store as data; never let body content steer AI or
  reach a shell/tool. HTML email is sanitized to text for storage and display.
- **PII.** Client emails contain personal and business data. It lives in the host
  SQLite under the existing `data/` hardening (chmod 600, operator-role gating on
  the routes). Document retention and a purge path (`DELETE` by client, by age).
- **Outbound is human-gated.** No auto-send to clients, ever, in v1. Every send is an
  explicit operator action with a trail row.
- **Attachments:** v1 stores metadata and a text note only; do not persist or
  execute attachment binaries. Defer real attachment handling.
- **Role floor:** reads for authenticated users; create/link/resolve/send at the
  operator role, matching the messages/errors delete posture.
- **Webhook auth:** the intake endpoint is a machine-ingest surface; gate with the
  X-API-Key or webhook token, same as the existing webhooks, before exposing the
  port.

## 14. API surface

`/api/support`, operator floor except reads and the token-gated intake:

| Method | Path | Purpose |
|---|---|---|
| POST | `/intake` | inbound email to ticket (token-gated) |
| GET | `/tickets` | list/filter (client, instance, status, linked/unlinked) |
| GET | `/tickets/{id}` | ticket detail + thread + trail |
| PATCH | `/tickets/{id}` | status, priority, links |
| POST | `/tickets/{id}/links` | confirm/set client/instance/workflow/execution/error |
| POST | `/tickets/{id}/issue` | create or link an issue from this ticket |
| POST | `/tickets/{id}/reply` | draft a reply (LLM optional) |
| POST | `/tickets/{id}/reply/send` | approve + send (n8n relay or SMTP) |
| GET | `/issues` | list/filter (status, severity, owner, instance) |
| GET | `/issues/{id}` | issue detail + linked tickets + trail |
| PATCH | `/issues/{id}` | owner, severity, status, root_cause, resolution, summary, include_in_report |
| GET | `/clients` / POST / PATCH | client CRUD (name, domains, instances) |
| GET | `/report-feed` | resolved, client-safe items for a reporting window |

Live updates: broadcast `support:ticket` and `support:issue` events over the
existing WebSocket so the view updates like the errors feed does.

## 15. Frontend

One view, `frontend/js/views/support.js`, ES module `render(container)`:

- **Inbox pane:** ticket list with a linked/unlinked filter and per-row link chips
  (client, instance, workflow, execution). Unlinked tickets surface first; the whole
  point is to drive them to linked.
- **Ticket detail:** thread, the link editor, "This is a real failure" to
  create/link an issue, reply composer with the LLM draft and an explicit Send.
- **Issues pane:** operational list (owner, severity, status), issue detail with
  root cause / resolution / client summary / include-in-report, and the linked
  tickets.
- **Trail:** a visible timeline on both ticket and issue, because the durable record
  is the product, not a hidden log.

## 16. Testing

- Intake: webhook creates a ticket; threading appends via `in_reply_to`; token
  gating enforced.
- Linking: domain to client, single-instance auto-link, execution-id and workflow-
  name extraction, issue-candidate proposal; every link writes a trail row.
- Issue dedupe: same `error_group_key` links rather than duplicates.
- Resolve fan-out: client_summary propagates to linked tickets; report-feed returns
  the item only when resolved and `include_in_report`.
- Outbound: draft never auto-sends; send writes `reply_sent`; failure writes
  `failed`.
- Security: HTML sanitized; unauthenticated intake without token rejected; operator
  floor on mutations; purge-by-client removes tickets, messages, and trail.

## 17. Milestones

| # | Deliverable | Est |
|---|---|---|
| M1 | Schema (`support_*`), ticket + trail CRUD, WebSocket events | 1 day |
| M2 | Intake webhook + threading | 0.5 day |
| M3 | Linking engine (client/instance/workflow/execution/error) | 1 day |
| M4 | Issue create/link + dedupe on error_group_key + fields | 1 day |
| M5 | Reply draft (bridge) + send (n8n relay / SMTP) + human gate | 1 day |
| M6 | Report-feed endpoint | 0.5 day |
| M7 | Frontend view (inbox, ticket, issues, trail) | 1.5 days |
| M8 | AI triage agent (optional, Agent Fleet) | 1 day |
| M9 | Consent/isolation wiring + docs + tests | 1 day |

Core desk without AI (M1 to M7, M9) is roughly 7 to 8 days. The AI triage agent
(M8) is additive.

## 18. Open questions

1. **Client entity depth.** Lightweight (domains + instance list) as specced, or a
   richer CRM-ish object? Leaning lightweight; agencies likely already have a CRM.
2. **Distribution.** Community module (per Michael) that runs in_process with
   consent, or promote to a core built-in given how deep it reads host data? Same
   question the secret backends raised; answer both together.
3. **Report generation ownership.** Does this module stop at the feed, or does a
   separate maintenance-report module/feature own rendering and delivery? Spec stops
   at the feed on purpose.
4. **IMAP intake priority.** Webhook-only for v1 keeps intake sandbox-clean; is a
   built-in mailbox poller wanted soon enough to pull it into v1?
5. **Fleet Health fold.** Blocked on the Fleet Health contribution API (host gap).
   Ship without the health row, add it when that lands?
```
