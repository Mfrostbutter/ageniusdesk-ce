# Executions & Errors

The Errors view is a real-time feed of failed n8n executions across your connected instances. n8n's global error handler posts each failure to AgeniusDesk, the dashboard stores it, broadcasts it over a WebSocket, and groups identical failures so a recurring problem shows up as one row with a count instead of hundreds of duplicates. This page covers what lands in the feed, how grouping and filtering work, how to run AI triage on an error, and how to install the global error handler.

Related: [Insights](insights.md) rolls the same execution data into analytics. Security posture for the public webhook is in [../architecture/security.md](../architecture/security.md).

## What lands in the feed

Errors arrive two ways:

| Source | How it gets there | Endpoint |
|---|---|---|
| Live n8n failures | The Global Error Handler workflow runs on every failed execution and POSTs the error to the dashboard | `POST /api/errors/webhook` |
| Backfill | On view load the dashboard pulls recent failed executions from n8n's API and fills any gaps | `POST /api/errors/sync` |

Each stored error carries: `workflow_id`, `workflow_name`, `execution_id`, `node_name`, `error_message`, `error_type`, and the `instance_id` of the n8n instance that was active when it arrived. The webhook tags the error with the currently active instance, so when you run multiple instances, run one error handler per instance for correct attribution.

When a webhook error is received it is persisted and broadcast over the WebSocket as an `error` event. The open Errors view reacts live: in Flat view the new row is prepended; in Grouped view the feed reloads so the counts stay honest. The sidebar error badge updates from the 24h count.

### Example webhook payload

The handler's Code node extracts these fields and the HTTP Request node POSTs them as JSON to `/api/errors/webhook`:

```json
{
  "workflow_id": "abc123",
  "workflow_name": "Daily Lead Sync",
  "execution_id": "98765",
  "node_name": "HTTP Request",
  "error_message": "connect ETIMEDOUT 10.0.0.5:443",
  "error_type": "NodeApiError"
}
```

All fields are optional. Missing fields fall back to defaults (`workflow_name` -> `"Unknown Workflow"`, `error_type` -> `"Error"`, `error_message` -> `"Unknown error"`).

## Grouped vs Flat view

A toggle at the top right switches between two views. The choice is remembered per browser session.

- **Grouped** (default): collapses errors by the key `(workflow, node, error_type)`. Each row shows a `×count` pill and the most recent occurrence as the sample. This is built server-side at `GET /api/errors/grouped` using window functions, so 80 identical OAuth failures become a single row. Expanded detail shows first-seen, last-seen, the last execution id, workflow id, and the last error message.
- **Flat**: every individual occurrence as its own row, newest first, from `GET /api/errors`.

Both views show a colored instance chip on each row so you know which n8n stack produced the error.

## Time range and filtering

A **Range** selector limits the feed to the last 24 hours, 7 days, or 30 days. The choice persists per browser session. The count label above the feed reads, for example, `42 in last 24h · 7 groups`.

The feed is scoped to the **active instance** by default. The underlying endpoints accept an `instance_id` query parameter:

| Value | Scope |
|---|---|
| `active` (default) | The currently active n8n instance |
| `all` | Every stored error across instances |
| a concrete id | That specific instance |

Counts returned in the payload use the same scope, so the badge matches the feed.

## AI triage of an error

Each error row (grouped or flat) has an **Ask AI** button in its expanded detail.

1. Expand an error row by clicking it.
2. Click **Ask AI**.
3. The dashboard builds a prompt from the error context (workflow name, failing node, error type, execution id, and the error message) and sends it to the assistant at `POST /api/assistant/chat` with `surface: "triage"`.
4. The analysis renders inline under the error with **Copy** and dismiss controls. Click **Ask AI** again to toggle it closed.

This uses the same assistant the Workflows page uses, so it needs an AI provider configured in the assistant module.

## Clearing and purging errors

| Action | Control | What it does |
|---|---|---|
| Delete one error | **Delete This Error** (Flat row) | Removes the local row by `execution_id` and, by default, purges that execution from n8n (`DELETE /api/errors/{execution_id}`). |
| Clear a group | **Clear Group (×n)** (Grouped row) | Deletes all local rows matching the group key. Hold **Shift** when confirming to also purge the matching executions from n8n (`POST /api/errors/clear-group`). |
| Clear all for a workflow | **Clear All for Workflow** (Flat row) | Deletes local rows for the workflow and purges its executions from n8n. |
| Clear everything | **Clear All** (header) | Clears all errors for the active instance (`DELETE /api/errors`). |

All clear actions are scoped to the active instance because execution ids are not globally unique across instances.

## Installing the global error handler

AgeniusDesk can install the error handler into your active n8n instance in one click. The onboarding flow ("Add error reporting") offers it right after you connect an instance; you can also trigger it from Settings -> Error Handler.

1. Open the install prompt. It first checks `GET /api/errors/handler-status` to see if a handler is already present.
2. If a handler already exists, the prompt says so and does nothing else. **It never creates a second copy.**
3. If none exists, click **Install error handler**. The dashboard imports the bundled workflow into the active instance and activates it (`POST /api/errors/install-handler`).
4. Finish the one manual step in n8n: **Settings -> Workflows -> Error Workflow**, then select **"Global Error Handler -> AgeniusDesk"**. n8n's public API cannot set the instance-wide Error Workflow, so this single selection is required for the handler to run on every failure.

### Idempotency

Install is safe to run repeatedly. The server looks for an existing workflow whose name contains "Global Error Handler" and, if found, reuses it (activating it if needed) instead of importing a duplicate. The response includes `already_existed: true` in that case.

### Reachable callback URL

The handler POSTs errors back to AgeniusDesk, so n8n must be able to reach the dashboard. When your browser reached AgeniusDesk at `localhost` but n8n lives on a LAN host, the dashboard substitutes a reachable host (reusing n8n's host with the dashboard port) into the workflow's HTTP node. The workflow also keeps an `$env.FLOW_DASHBOARD_URL` fallback so you can override the URL inside n8n if needed.

You can download the workflow JSON (URL pre-filled) from the Error Handler settings tab via `GET /api/errors/handler-template` if you prefer to import it manually.

## Securing the webhook on public deployments

`POST /api/errors/webhook` is a legacy webhook route. By default it is open so the in-n8n handler can post without credentials. On a public deployment, set **`AGD_WEBHOOK_TOKEN`** so the dashboard rejects unauthenticated webhook posts.

When `AGD_WEBHOOK_TOKEN` is set, every request to `/api/errors/webhook` (and `/api/messages/webhook`) must present the token, either as:

```
X-AGD-Webhook-Token: <token>
```

or as a bearer token:

```
Authorization: Bearer <token>
```

Requests without a valid token get `401 Invalid or missing webhook token`. If you set this, configure the same token on the HTTP Request node in your n8n error handler. Leaving `AGD_WEBHOOK_TOKEN` empty keeps the webhook open, which is acceptable only behind an authenticated edge (for example a Cloudflare Access tunnel). See [../architecture/security.md](../architecture/security.md).
